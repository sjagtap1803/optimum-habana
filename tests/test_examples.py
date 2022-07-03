# coding=utf-8
# Copyright 2022 HuggingFace Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import re
import subprocess
from distutils.util import strtobool
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Dict, List, Optional, Tuple, Union
from unittest import TestCase

from transformers import (
    CONFIG_MAPPING,
    MODEL_FOR_CAUSAL_LM_MAPPING,
    MODEL_FOR_QUESTION_ANSWERING_MAPPING,
    MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING,
    MODEL_FOR_SEQUENCE_CLASSIFICATION_MAPPING,
)
from transformers.testing_utils import slow

from .utils import (
    MODELS_TO_TEST_MAPPING,
    VALID_MODELS_FOR_LANGUAGE_MODELING,
    VALID_MODELS_FOR_QUESTION_ANSWERING,
    VALID_MODELS_FOR_SEQUENCE_CLASSIFICATION,
    VALID_SEQ2SEQ_MODELS,
)


BASELINE_DIRECTORY = Path(__file__).parent.resolve() / Path("baselines")
# Models should reach at least 99% of their baseline accuracy
ACCURACY_PERF_FACTOR = 0.99
# Trainings should last at most 5% longer than the baseline
TRAINING_TIME_PERF_FACTOR = 1.05


def _get_supported_models_for_script(
    models_to_test: Dict[str, List[Tuple[str]]],
    task_mapping: Dict[str, str],
    valid_models_for_task: List[str],
) -> List[Tuple[str]]:
    """
    Filter models that can perform the task from models_to_test.
    Args:
        models_to_test: mapping between a model type and a tuple (model_name_or_path, gaudi_config_name).
        task_mapping: mapping between a model config and a model class.
        valid_models_for_task: list of supported models for a specific task.
    Returns:
        A list of models that are supported for the task.
        Each element of the list follows the same format: (model_type, (model_name_or_path, gaudi_config_name)).
    """

    def is_valid_model_type(model_type: str) -> bool:
        in_task_mapping = CONFIG_MAPPING[model_type] in task_mapping
        in_valid_models_for_task = model_type in valid_models_for_task
        if in_task_mapping and in_valid_models_for_task:
            return True
        return False

    return [
        model for model_type, models in models_to_test.items() for model in models if is_valid_model_type(model_type)
    ]


_SCRIPT_TO_MODEL_MAPPING = {
    "run_qa": _get_supported_models_for_script(
        MODELS_TO_TEST_MAPPING,
        MODEL_FOR_QUESTION_ANSWERING_MAPPING,
        VALID_MODELS_FOR_QUESTION_ANSWERING,
    ),
    "run_glue": _get_supported_models_for_script(
        MODELS_TO_TEST_MAPPING,
        MODEL_FOR_SEQUENCE_CLASSIFICATION_MAPPING,
        VALID_MODELS_FOR_SEQUENCE_CLASSIFICATION,
    ),
    "run_clm": _get_supported_models_for_script(
        MODELS_TO_TEST_MAPPING,
        MODEL_FOR_CAUSAL_LM_MAPPING,
        VALID_MODELS_FOR_LANGUAGE_MODELING,
    ),
    "run_summarization": _get_supported_models_for_script(
        MODELS_TO_TEST_MAPPING,
        MODEL_FOR_SEQ_TO_SEQ_CAUSAL_LM_MAPPING,
        VALID_SEQ2SEQ_MODELS,
    ),
}


class ExampleTestMeta(type):
    """
    Metaclass that takes care of creating the proper example tests for a given task.
    It uses example_name to figure out which models support this task, and create a run example test for each of these
    models.
    """

    def __new__(cls, name, bases, attrs, example_name=None, multi_card=False):
        if example_name is not None:
            models_to_test = _SCRIPT_TO_MODEL_MAPPING.get(example_name)
            if models_to_test is None:
                raise AttributeError(f"Could not create class because no model was found for example {example_name}")
        for model_name, gaudi_config_name in models_to_test:
            # Conditional statement to filter out ALBERT XXL 1x if env variable RUN_ALBERT_XXL_1X is not true
            test_albert_xxl_1x = ("RUN_ALBERT_XXL_1X" in os.environ) and strtobool(os.environ["RUN_ALBERT_XXL_1X"])
            if model_name != "albert-xxlarge-v1" or multi_card or test_albert_xxl_1x:
                attrs[
                    f"test_{example_name}_{model_name}_{'multi_card' if multi_card else 'single_card'}"
                ] = cls._create_test(model_name, gaudi_config_name, multi_card)
        attrs["EXAMPLE_NAME"] = example_name
        return super().__new__(cls, name, bases, attrs)

    @classmethod
    def _create_test(cls, model_name: str, gaudi_config_name: str, multi_card: bool = False) -> Callable[[], None]:
        """
        Create a test function that runs an example for a specific (model_name, gaudi_config_name) pair.
        Args:
            model_name (str): the model_name_or_path.
            gaudi_config_name (str): the gaudi config name.
            multi_card (bool): whether it is a distributed run or not.
        Returns:
            The test function that runs the example.
        """

        @slow
        def test(self):
            if self.EXAMPLE_NAME is None:
                raise ValueError("An example name must be provided")
            example_script = Path(self.EXAMPLE_DIR).glob(f"*/{self.EXAMPLE_NAME}.py")
            example_script = list(example_script)
            if len(example_script) == 0:
                raise RuntimeError(f"Could not find {self.EXAMPLE_NAME}.py in examples located in {self.EXAMPLE_DIR}")
            elif len(example_script) > 1:
                raise RuntimeError(f"Found more than {self.EXAMPLE_NAME}.py in examples located in {self.EXAMPLE_DIR}")
            else:
                example_script = example_script[0]

            self._install_requirements(example_script.parent / "requirements.txt")

            path_to_baseline = BASELINE_DIRECTORY / Path(model_name.replace("-", "_")).with_suffix(".json")
            with path_to_baseline.open("r") as json_file:
                baseline = json.load(json_file)[self.TASK_NAME]

            distribution = "multi_card" if multi_card else "single_card"

            with TemporaryDirectory() as tmp_dir:
                cmd_line = self._create_command_line(
                    multi_card,
                    example_script,
                    model_name,
                    gaudi_config_name,
                    tmp_dir,
                    task=self.TASK_NAME,
                    lr=baseline.get("distribution").get(distribution).get("learning_rate"),
                    train_batch_size=baseline.get("distribution").get(distribution).get("train_batch_size"),
                    eval_batch_size=baseline.get("eval_batch_size"),
                    num_epochs=baseline.get("num_train_epochs"),
                    extra_command_line_arguments=baseline.get("distribution")
                    .get(distribution)
                    .get("extra_arguments", []),
                )

                p = subprocess.Popen(cmd_line)
                return_code = p.wait()

                # Ensure the run finished without any issue
                self.assertEqual(return_code, 0)

                with open(Path(tmp_dir) / "all_results.json") as fp:
                    results = json.load(fp)

                # Ensure performance requirements (accuracy, training time) are met
                self.assert_no_regression(results, baseline.get("distribution").get(distribution))

            # TODO: is a cleanup of the dataset cache needed?
            # self._cleanup_dataset_cache()

        return test


class ExampleTesterBase(TestCase):
    """
    Base example tester class.
    Attributes:
        EXAMPLE_DIR (`str` or `os.Pathlike`): the directory containing the examples.
        EXAMPLE_NAME (`str`): the name of the example script without the file extension, e.g. run_qa, run_glue, etc.
        TASK_NAME (`str`): the name of the dataset to use.
        DATASET_PARAMETER_NAME (`str`): the argument name to use for the dataset parameter.
            Most of the time it will be "dataset_name", but for some tasks on a benchmark it might be something else.
        MAX_SEQ_LENGTH ('str'): the max_seq_length argument for this dataset.
            The maximum total input sequence length after tokenization. Sequences longer than this will be truncated, sequences shorter will be padded.
    """

    EXAMPLE_DIR = Path(os.path.dirname(__file__)).parent / "examples"
    EXAMPLE_NAME = None
    TASK_NAME = None
    DATASET_PARAMETER_NAME = "dataset_name"
    REGRESSION_METRICS = {
        "eval_f1": (TestCase.assertGreaterEqual, ACCURACY_PERF_FACTOR),
        "perplexity": (TestCase.assertLessEqual, 2 - ACCURACY_PERF_FACTOR),
        "eval_rougeLsum": (TestCase.assertGreaterEqual, ACCURACY_PERF_FACTOR),
        "train_runtime": (TestCase.assertLessEqual, TRAINING_TIME_PERF_FACTOR),
    }

    def _create_command_line(
        self,
        multi_card: bool,
        script: Path,
        model_name: str,
        gaudi_config_name: str,
        output_dir: str,
        lr: float,
        train_batch_size: int,
        eval_batch_size: int,
        num_epochs: int,
        task: Optional[str] = None,
        extra_command_line_arguments: Optional[List[str]] = None,
    ) -> List[str]:
        task_option = f"--{self.DATASET_PARAMETER_NAME} {task}" if task else " "

        cmd_line = ["python3"]
        if multi_card:
            cmd_line.append(f"{script.parent.parent / 'gaudi_spawn.py'}")
            cmd_line.append("--world_size 8")
            cmd_line.append("--use_mpi")

        cmd_line += [
            f"{script}",
            f"--model_name_or_path {model_name}",
            f"--gaudi_config_name {gaudi_config_name}",
            f"{task_option}",
            "--do_train",
            "--do_eval",
            f"--output_dir {output_dir}",
            "--overwrite_output_dir",
            f"--learning_rate {lr}",
            f"--per_device_train_batch_size {train_batch_size}",
            f"--per_device_eval_batch_size {eval_batch_size}",
            f" --num_train_epochs {num_epochs}",
            "--use_habana",
            "--use_lazy_mode",
            "--throughput_warmup_steps 2",
        ]

        if extra_command_line_arguments is not None:
            cmd_line += extra_command_line_arguments

        pattern = re.compile(r"([\"\'].+?[\"\'])|\s")
        return [x for y in cmd_line for x in re.split(pattern, y) if x]

    def _install_requirements(self, requirements_filename: Union[str, os.PathLike]):
        """
        Installs the necessary requirements to run the example if the provided file exists, otherwise does nothing.
        """

        if not Path(requirements_filename).exists():
            return

        cmd_line = f"pip install -r {requirements_filename}".split()
        p = subprocess.Popen(cmd_line)
        return_code = p.wait()
        self.assertEqual(return_code, 0)

    def assert_no_regression(self, results: Dict, baseline: Dict):
        """
        Assert whether all possible performance requirements are met.
        Attributes:
            results (Dict): results of the run to assess
            baseline (Dict): baseline to assert whether or not there is regression
        """

        for metric_name, assert_function_and_threshold in self.REGRESSION_METRICS.items():
            if metric_name in baseline:
                assert_function, threshold_factor = assert_function_and_threshold
                assert_function(self, results[metric_name], threshold_factor * baseline[metric_name])


class TextClassificationExampleTester(ExampleTesterBase, metaclass=ExampleTestMeta, example_name="run_glue"):
    TASK_NAME = "mrpc"
    DATASET_PARAMETER_NAME = "task_name"


class MultiCardTextClassificationExampleTester(
    ExampleTesterBase, metaclass=ExampleTestMeta, example_name="run_glue", multi_card=True
):
    TASK_NAME = "mrpc"
    DATASET_PARAMETER_NAME = "task_name"


class QuestionAnsweringExampleTester(ExampleTesterBase, metaclass=ExampleTestMeta, example_name="run_qa"):
    TASK_NAME = "squad"


class MultiCardQuestionAnsweringExampleTester(
    ExampleTesterBase, metaclass=ExampleTestMeta, example_name="run_qa", multi_card=True
):
    TASK_NAME = "squad"


class LanguageModelingExampleTester(ExampleTesterBase, metaclass=ExampleTestMeta, example_name="run_clm"):
    TASK_NAME = "wikitext"


class MultiCardLanguageModelingExampleTester(
    ExampleTesterBase, metaclass=ExampleTestMeta, example_name="run_clm", multi_card=True
):
    TASK_NAME = "wikitext"


class MultiCardSummarizationExampleTester(
    ExampleTesterBase, metaclass=ExampleTestMeta, example_name="run_summarization", multi_card=True
):
    TASK_NAME = "cnn_dailymail"
