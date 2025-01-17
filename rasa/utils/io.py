import asyncio
import errno
import logging
import os
import tarfile
import tempfile
import warnings
import zipfile
from asyncio import AbstractEventLoop
from typing import Text, Any, Dict, Union, List, Type
import ruamel.yaml as yaml
from io import BytesIO as IOReader

import simplejson
import typing

from rasa.constants import ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL

if typing.TYPE_CHECKING:
    from prompt_toolkit.validation import Validator


def configure_colored_logging(loglevel):
    import coloredlogs

    loglevel = loglevel or os.environ.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL)

    field_styles = coloredlogs.DEFAULT_FIELD_STYLES.copy()
    field_styles["asctime"] = {}
    level_styles = coloredlogs.DEFAULT_LEVEL_STYLES.copy()
    level_styles["debug"] = {}
    coloredlogs.install(
        level=loglevel,
        use_chroot=False,
        fmt="%(asctime)s %(levelname)-8s %(name)s  - %(message)s",
        level_styles=level_styles,
        field_styles=field_styles,
    )


def enable_async_loop_debugging(event_loop: AbstractEventLoop) -> AbstractEventLoop:
    logging.info(
        "Enabling coroutine debugging. Loop id {}.".format(id(asyncio.get_event_loop()))
    )

    # Enable debugging
    event_loop.set_debug(True)

    # Make the threshold for "slow" tasks very very small for
    # illustration. The default is 0.1 (= 100 milliseconds).
    event_loop.slow_callback_duration = 0.001

    # Report all mistakes managing asynchronous resources.
    warnings.simplefilter("always", ResourceWarning)
    return event_loop


def fix_yaml_loader() -> None:
    """Ensure that any string read by yaml is represented as unicode."""

    def construct_yaml_str(self, node):
        # Override the default string handling function
        # to always return unicode objects
        return self.construct_scalar(node)

    yaml.Loader.add_constructor("tag:yaml.org,2002:str", construct_yaml_str)
    yaml.SafeLoader.add_constructor("tag:yaml.org,2002:str", construct_yaml_str)


def replace_environment_variables():
    """Enable yaml loader to process the environment variables in the yaml."""
    import re
    import os

    # eg. ${USER_NAME}, ${PASSWORD}
    env_var_pattern = re.compile(r"^(.*)\$\{(.*)\}(.*)$")
    yaml.add_implicit_resolver("!env_var", env_var_pattern)

    def env_var_constructor(loader, node):
        """Process environment variables found in the YAML."""
        value = loader.construct_scalar(node)
        expanded_vars = os.path.expandvars(value)
        if "$" in expanded_vars:
            not_expanded = [w for w in expanded_vars.split() if "$" in w]
            raise ValueError(
                "Error when trying to expand the environment variables"
                " in '{}'. Please make sure to also set these environment"
                " variables: '{}'.".format(value, not_expanded)
            )
        return expanded_vars

    yaml.SafeConstructor.add_constructor("!env_var", env_var_constructor)


def read_yaml(content: Text) -> Union[List[Any], Dict[Text, Any]]:
    """Parses yaml from a text.

     Args:
        content: A text containing yaml content.
    """
    fix_yaml_loader()
    replace_environment_variables()

    yaml_parser = yaml.YAML(typ="safe")
    yaml_parser.version = "1.2"
    yaml_parser.unicode_supplementary = True

    # noinspection PyUnresolvedReferences
    try:
        return yaml_parser.load(content) or {}
    except yaml.scanner.ScannerError:
        # A `ruamel.yaml.scanner.ScannerError` might happen due to escaped
        # unicode sequences that form surrogate pairs. Try converting the input
        # to a parsable format based on
        # https://stackoverflow.com/a/52187065/3429596.
        content = (
            content.encode("utf-8")
            .decode("raw_unicode_escape")
            .encode("utf-16", "surrogatepass")
            .decode("utf-16")
        )
        return yaml_parser.load(content) or {}


def read_file(filename: Text, encoding: Text = "utf-8") -> Any:
    """Read text from a file."""

    try:
        with open(filename, encoding=encoding) as f:
            return f.read()
    except FileNotFoundError:
        raise ValueError("File '{}' does not exist.".format(filename))


def read_json_file(filename: Text) -> Any:
    """Read json from a file."""
    content = read_file(filename)
    try:
        return simplejson.loads(content)
    except ValueError as e:
        raise ValueError(
            "Failed to read json from '{}'. Error: "
            "{}".format(os.path.abspath(filename), e)
        )


def read_config_file(filename: Text) -> Dict[Text, Any]:
    """Parses a yaml configuration file. Content needs to be a dictionary

     Args:
        filename: The path to the file which should be read.
    """
    content = read_yaml(read_file(filename, "utf-8"))

    if content is None:
        return {}
    elif isinstance(content, dict):
        return content
    else:
        raise ValueError(
            "Tried to load invalid config file '{}'. "
            "Expected a key value mapping but found {}"
            ".".format(filename, type(content))
        )


def read_yaml_file(filename: Text) -> Union[List[Any], Dict[Text, Any]]:
    """Parses a yaml file.

     Args:
        filename: The path to the file which should be read.
    """
    return read_yaml(read_file(filename, "utf-8"))


def unarchive(byte_array: bytes, directory: Text) -> Text:
    """Tries to unpack a byte array interpreting it as an archive.

    Tries to use tar first to unpack, if that fails, zip will be used."""

    try:
        tar = tarfile.open(fileobj=IOReader(byte_array))
        tar.extractall(directory)
        tar.close()
        return directory
    except tarfile.TarError:
        zip_ref = zipfile.ZipFile(IOReader(byte_array))
        zip_ref.extractall(directory)
        zip_ref.close()
        return directory


def write_yaml_file(data: Dict, filename: Text):
    """Writes a yaml file.

     Args:
        data: The data to write.
        filename: The path to the file which should be written.
    """
    with open(filename, "w", encoding="utf-8") as outfile:
        yaml.dump(data, outfile, default_flow_style=False)


def is_subdirectory(path: Text, potential_parent_directory: Text) -> bool:
    if path is None or potential_parent_directory is None:
        return False

    path = os.path.abspath(path)
    potential_parent_directory = os.path.abspath(potential_parent_directory)

    return potential_parent_directory in path


def create_temporary_file(data: Any, suffix: Text = "", mode: Text = "w+") -> Text:
    """Creates a tempfile.NamedTemporaryFile object for data.

    mode defines NamedTemporaryFile's  mode parameter in py3."""

    encoding = None if "b" in mode else "utf-8"
    f = tempfile.NamedTemporaryFile(
        mode=mode, suffix=suffix, delete=False, encoding=encoding
    )
    f.write(data)

    f.close()
    return f.name


def create_path(file_path: Text):
    """Makes sure all directories in the 'file_path' exists."""

    parent_dir = os.path.dirname(os.path.abspath(file_path))
    if not os.path.exists(parent_dir):
        os.makedirs(parent_dir)


def zip_folder(folder: Text) -> Text:
    """Create an archive from a folder."""
    import tempfile
    import shutil

    zipped_path = tempfile.NamedTemporaryFile(delete=False)
    zipped_path.close()

    # WARN: not thread save!
    return shutil.make_archive(zipped_path.name, str("zip"), folder)


def create_directory_for_file(file_path: Text) -> None:
    """Creates any missing parent directories of this file path."""

    try:
        os.makedirs(os.path.dirname(file_path))
    except OSError as e:
        # be happy if someone already created the path
        if e.errno != errno.EEXIST:
            raise


def questionary_file_path_validator(
    valid_file_types: List[Text], error_message: Text
) -> Type["Validator"]:
    """Creates a `Validator` class which can be used with `questionary` to validate
       file paths.
    """

    from prompt_toolkit.validation import Validator, ValidationError
    from prompt_toolkit.document import Document

    class ExportPathValidator(Validator):
        def validate(self, document: Document) -> None:
            path = document.text
            is_valid = path is not None and any(
                [path.endswith(file_type) for file_type in valid_file_types]
            )
            if not is_valid:
                raise ValidationError(message=error_message)

    return ExportPathValidator
