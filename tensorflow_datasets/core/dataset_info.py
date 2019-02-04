# coding=utf-8
# Copyright 2018 The TensorFlow Datasets Authors.
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

"""DatasetInfo records the information we know about a dataset.

This includes things that we know about the dataset statically, i.e.:
 - schema
 - description
 - canonical location
 - does it have validation and tests splits
 - size
 - etc.

This also includes the things that can and should be computed once we've
processed the dataset as well:
 - number of examples (in each split)
 - feature statistics (in each split)
 - etc.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import os
import pprint

from absl import logging
import numpy as np
import tensorflow as tf

from tensorflow_datasets.core import api_utils
from tensorflow_datasets.core import constants
from tensorflow_datasets.core import dataset_utils
from tensorflow_datasets.core import splits as splits_lib
from tensorflow_datasets.core import utils
from tensorflow_datasets.core.proto import dataset_info_pb2
import tqdm
from google.protobuf import json_format
from tensorflow_metadata.proto.v0 import schema_pb2
from tensorflow_metadata.proto.v0 import statistics_pb2


# Name of the file to output the DatasetInfo protobuf object.
DATASET_INFO_FILENAME = "dataset_info.json"

INFO_STR = """tfds.core.DatasetInfo(
    name='{name}',
    version={version},
    description='{description}',
    urls={urls},
    features={features},
    total_num_examples={total_num_examples},
    splits={splits},
    supervised_keys={supervised_keys},
    citation='{citation}',
)
"""


# TODO(tfds): Do we require to warn the user about the peak memory used while
# constructing the dataset?
class DatasetInfo(object):
  """Information about a dataset.

  `DatasetInfo` documents datasets, including its name, version, and features.
  See the constructor arguments and properties for a full list.

  Note: Not all fields are known on construction and may be updated later
  by `compute_dynamic_properties`. For example, the number of examples in each
  split is typically updated during data generation (i.e. on calling
  `builder.download_and_prepare()`).
  """

  @api_utils.disallow_positional_args
  def __init__(self,
               builder,
               description=None,
               features=None,
               supervised_keys=None,
               splits=None,
               urls=None,
               download_checksums=None,
               size_in_bytes=0,
               citation=None):
    """Constructs DatasetInfo.

    Args:
      builder: `DatasetBuilder`, dataset builder for this info.
      description: `str`, description of this dataset.
      features: `tfds.features.FeaturesDict`, Information on the feature dict
        of the `tf.data.Dataset()` object from the `builder.as_dataset()`
        method.
      supervised_keys: `tuple`, Specifies the input feature and the label for
        supervised learning, if applicable for the dataset.
      splits: `tfds.core.SplitDict`, the available splits for this dataset.
      urls: `list(str)`, optional, the homepage(s) for this dataset.
      download_checksums: `dict<str url, str sha256>`, URL to sha256 of file.
        If a url is not listed, its checksum is not checked.
      size_in_bytes: `int`, optional, approximate size in bytes of the raw
        size of the dataset that we will be downloading from the internet.
      citation: `str`, optional, the citation to use for this dataset.
    """
    self._builder = builder

    self._info_proto = dataset_info_pb2.DatasetInfo(
        name=builder.name,
        description=description,
        version=str(builder._version),  # pylint: disable=protected-access
        size_in_bytes=int(size_in_bytes),
        citation=citation)
    if urls:
      self._info_proto.location.urls[:] = urls
    self._info_proto.download_checksums.update(download_checksums or {})

    self._features = features
    self._splits = splits or splits_lib.SplitDict()
    if supervised_keys is not None:
      assert isinstance(supervised_keys, tuple)
      assert len(supervised_keys) == 2
      self._info_proto.supervised_keys.input = supervised_keys[0]
      self._info_proto.supervised_keys.output = supervised_keys[1]

    # Is this object initialized with both the static and the dynamic data?
    self._fully_initialized = False

  @property
  def as_proto(self):
    return self._info_proto

  @property
  def name(self):
    return self.as_proto.name

  @property
  def description(self):
    return self.as_proto.description

  @property
  def version(self):
    return utils.Version(self.as_proto.version)

  @property
  def citation(self):
    return self.as_proto.citation

  @property
  def size_in_bytes(self):
    return self.as_proto.size_in_bytes

  @size_in_bytes.setter
  def size_in_bytes(self, size):
    self.as_proto.size_in_bytes = size

  @property
  def features(self):
    return self._features

  @property
  def supervised_keys(self):
    if not self.as_proto.HasField("supervised_keys"):
      return None
    supervised_keys = self.as_proto.supervised_keys
    return (supervised_keys.input, supervised_keys.output)

  @property
  def splits(self):
    return self._splits.copy()

  @splits.setter
  def splits(self, split_dict):
    assert isinstance(split_dict, splits_lib.SplitDict)

    # Update the dictionary representation.
    # Use from/to proto for a clean copy
    self._splits = split_dict.copy()

    # Update the proto
    del self.as_proto.splits[:]  # Clear previous
    for split_info in split_dict.to_proto():
      self.as_proto.splits.add().CopyFrom(split_info)

  @property
  def urls(self):
    return self.as_proto.location.urls

  @property
  def download_checksums(self):
    return self.as_proto.download_checksums

  @download_checksums.setter
  def download_checksums(self, checksums):
    self.as_proto.download_checksums.clear()
    self.as_proto.download_checksums.update(checksums)

  @property
  def initialized(self):
    """Whether DatasetInfo has been fully initialized."""
    return self._fully_initialized

  def _dataset_info_filename(self, dataset_info_dir):
    return os.path.join(dataset_info_dir, DATASET_INFO_FILENAME)

  def compute_dynamic_properties(self):
    self._compute_dynamic_properties(self._builder)
    self._fully_initialized = True

  def _compute_dynamic_properties(self, builder):
    """Update from the DatasetBuilder."""
    # Fill other things by going over the dataset.
    splits = self.splits
    for split_info in tqdm.tqdm(
        splits.values(), desc="Computing statistics...", unit=" split"):
      try:
        split_name = split_info.name
        # Fill DatasetFeatureStatistics.
        dataset_feature_statistics, schema = get_dataset_feature_statistics(
            builder, split_name)

        # Add the statistics to this split.
        split_info.statistics.CopyFrom(dataset_feature_statistics)

        # Set the schema at the top-level since this is independent of the
        # split.
        self.as_proto.schema.CopyFrom(schema)

      except tf.errors.InvalidArgumentError:
        # This means there is no such split, even though it was specified in the
        # info, the least we can do is to log this.
        logging.error(("%s's info() property specifies split %s, but it "
                       "doesn't seem to have been generated. Please ensure "
                       "that the data was downloaded for this split and re-run "
                       "download_and_prepare."), self.name, split_name)
        raise

    # Set splits to trigger proto update in setter
    self.splits = splits

  @property
  def as_json(self):
    return json_format.MessageToJson(self.as_proto, sort_keys=True)

  def write_to_directory(self, dataset_info_dir):
    """Write `DatasetInfo` as JSON to `dataset_info_dir`."""
    # Save the metadata from the features (vocabulary, labels,...)
    if self.features:
      self.features.save_metadata(dataset_info_dir)

    with tf.io.gfile.GFile(self._dataset_info_filename(dataset_info_dir),
                           "w") as f:
      f.write(self.as_json)

  def read_from_directory(self, dataset_info_dir, from_bucket=False):
    """Update DatasetInfo from the JSON file in `dataset_info_dir`.

    This function updates all the dynamically generated fields (num_examples,
    hash, time of creation,...) of the DatasetInfo.

    This will overwrite all previous metadata.

    Args:
      dataset_info_dir: `str` The directory containing the metadata file. This
        should be the root directory of a specific dataset version.
      from_bucket: `bool`, If data is restored from info files on GCS,
        then only the informations not defined in the code are updated

    Returns:
      True if we were able to initialize using `dataset_info_dir`, else false.
    """
    if not dataset_info_dir:
      raise ValueError(
          "Calling read_from_directory with undefined dataset_info_dir.")

    json_filename = self._dataset_info_filename(dataset_info_dir)

    # Load the metadata from disk
    if not tf.io.gfile.exists(json_filename):
      return False
    parsed_proto = read_from_json(json_filename)

    # Update splits
    self.splits = splits_lib.SplitDict.from_proto(parsed_proto.splits)
    # Update schema
    self.as_proto.schema.CopyFrom(parsed_proto.schema)
    # Restore the feature metadata (vocabulary, labels names,...)
    if self.features:
      self.features.load_metadata(dataset_info_dir)
    # Restore download info
    self.download_checksums = parsed_proto.download_checksums
    self.size_in_bytes = parsed_proto.size_in_bytes

    # If we are restoring on-disk data, then we also restore all dataset info
    # information from the previously saved proto.
    # If we are loading from the GCS bucket (only possible when we do not
    # restore previous data), then do not restore the info which are already
    # defined in the code. Otherwise, we would overwrite code info.
    if not from_bucket:
      # Update the full proto
      self._info_proto = parsed_proto

    if self._builder._version != self.version:  # pylint: disable=protected-access
      raise AssertionError(
          "The constructed DatasetInfo instance and the restored proto version "
          "do not match. Builder version: {}. Proto version: {}".format(
              self._builder._version, self.version))  # pylint: disable=protected-access

    # Mark as fully initialized.
    self._fully_initialized = True

    return True

  def initialize_from_bucket(self):
    """Initialize DatasetInfo from GCS bucket info files."""
    info_path = os.path.join(constants.DATASET_INFO_BUCKET, self.name)
    if self._builder.builder_config:
      info_path = os.path.join(info_path, self._builder.builder_config.name)
    info_path = os.path.join(info_path, str(self.version))
    return self.read_from_directory(info_path, from_bucket=True)

  def __repr__(self):
    return "<tfds.core.DatasetInfo name={name}, proto={{\n{proto}}}>".format(
        name=self.name, proto=repr(self.as_proto))

  def __str__(self):
    splits_pprint = "{\n %s\n    }" % (
        pprint.pformat(
            {k: self.splits[k] for k in sorted(list(self.splits.keys()))},
            indent=8, width=1)[1:-1])
    features_dict = self.features
    features_pprint = "%s({\n %s\n    }" % (
        type(features_dict).__name__,
        pprint.pformat({
            k: features_dict[k] for k in sorted(list(features_dict.keys()))
        }, indent=8, width=1)[1:-1])
    citation_pprint = '"""\n%s\n    """' % "\n".join(
        [u" " * 8 + line for line in self.citation.split(u"\n")])
    return INFO_STR.format(
        name=self.name,
        version=self.version,
        description=self.description,
        total_num_examples=self.splits.total_num_examples,
        features=features_pprint,
        splits=splits_pprint,
        citation=citation_pprint,
        urls=self.urls,
        supervised_keys=self.supervised_keys)

#
#
# This part is quite a bit messy and can be easily simplified with TFDV
# libraries, we can cut down the complexity by implementing cases on a need to
# do basis.
#
# My understanding of possible TF's types and shapes and kinds
# (ex: SparseTensor) is limited, please shout hard and guide on implementation.
#
#

_FEATURE_TYPE_MAP = {
    tf.float16: schema_pb2.FLOAT,
    tf.float32: schema_pb2.FLOAT,
    tf.float64: schema_pb2.FLOAT,
    tf.int8: schema_pb2.INT,
    tf.int16: schema_pb2.INT,
    tf.int32: schema_pb2.INT,
    tf.int64: schema_pb2.INT,
    tf.uint8: schema_pb2.INT,
    tf.uint16: schema_pb2.INT,
    tf.uint32: schema_pb2.INT,
    tf.uint64: schema_pb2.INT,
}

_SCHEMA_TYPE_MAP = {
    schema_pb2.INT: statistics_pb2.FeatureNameStatistics.INT,
    schema_pb2.FLOAT: statistics_pb2.FeatureNameStatistics.FLOAT,
    schema_pb2.BYTES: statistics_pb2.FeatureNameStatistics.BYTES,
    schema_pb2.STRUCT: statistics_pb2.FeatureNameStatistics.STRUCT,
}


# TODO(afrozm): What follows below can *VERY EASILY* be done by TFDV - rewrite
# this section once they are python 3 ready.
def get_dataset_feature_statistics(builder, split):
  """Calculate statistics for the specified split."""
  statistics = statistics_pb2.DatasetFeatureStatistics()

  # Make this to the best of our abilities.
  schema = schema_pb2.Schema()

  dataset = builder.as_dataset(split=split)

  # Just computing the number of examples for now.
  statistics.num_examples = 0

  # Feature dictionaries.
  feature_to_num_examples = collections.defaultdict(int)
  feature_to_min = {}
  feature_to_max = {}

  np_dataset = dataset_utils.as_numpy(dataset)
  for example in tqdm.tqdm(np_dataset, unit=" examples"):
    statistics.num_examples += 1

    assert isinstance(example, dict)

    feature_names = sorted(example.keys())
    for feature_name in feature_names:
      feature_np = example[feature_name]
      # For compatibility in graph and eager mode, we can get PODs here and
      # everything may not be neatly wrapped up in numpy's ndarray.
      feature_dtype = type(feature_np)
      if isinstance(feature_np, np.ndarray):
        if not feature_np.size:
          continue
        feature_dtype = feature_np.dtype.type

      # Update the number of examples this feature appears in.
      feature_to_num_examples[feature_name] += 1

      feature_min, feature_max = None, None
      is_numeric = (np.issubdtype(feature_dtype, np.number) or
                    feature_dtype == np.bool_)
      if is_numeric:
        feature_min = np.min(feature_np)
        feature_max = np.max(feature_np)

      # TODO(afrozm): What if shapes don't match? Populate ValueCount? Add
      # logic for that.

      # Set or update the min, max.
      if is_numeric:
        if ((feature_name not in feature_to_min) or
            (feature_to_min[feature_name] > feature_min)):
          feature_to_min[feature_name] = feature_min

        if ((feature_name not in feature_to_max) or
            (feature_to_max[feature_name] < feature_max)):
          feature_to_max[feature_name] = feature_max

  # Start here, we've processed all examples.

  output_shapes_dict = dataset.output_shapes
  output_types_dict = dataset.output_types

  for feature_name in sorted(feature_to_num_examples.keys()):
    # Try to fill in the schema.
    feature = schema.feature.add()
    feature.name = feature_name

    # TODO(afrozm): Make this work with nested structures, currently the Schema
    # proto has no support for it.
    maybe_feature_shape = output_shapes_dict[feature_name]
    if not isinstance(maybe_feature_shape, tf.TensorShape):
      logging.error(
          "Statistics generation doesn't work for nested structures yet")
      continue

    for dim in maybe_feature_shape.as_list():
      # We denote `None`s as -1 in the shape proto.
      feature.shape.dim.add().size = dim if dim else -1
    feature_type = output_types_dict[feature_name]
    feature.type = _FEATURE_TYPE_MAP.get(feature_type, schema_pb2.BYTES)

    common_statistics = statistics_pb2.CommonStatistics()
    common_statistics.num_non_missing = feature_to_num_examples[feature_name]
    common_statistics.num_missing = (
        statistics.num_examples - common_statistics.num_non_missing)

    feature_name_statistics = statistics.features.add()
    feature_name_statistics.name = feature_name

    # TODO(afrozm): This can be skipped, since type information was added to
    # the Schema.
    feature_name_statistics.type = _SCHEMA_TYPE_MAP.get(
        feature.type, statistics_pb2.FeatureNameStatistics.BYTES)

    if feature.type == schema_pb2.INT or feature.type == schema_pb2.FLOAT:
      numeric_statistics = statistics_pb2.NumericStatistics()
      numeric_statistics.min = feature_to_min[feature_name]
      numeric_statistics.max = feature_to_max[feature_name]
      numeric_statistics.common_stats.CopyFrom(common_statistics)
      feature_name_statistics.num_stats.CopyFrom(numeric_statistics)
    else:
      # Let's shove it into BytesStatistics for now.
      bytes_statistics = statistics_pb2.BytesStatistics()
      bytes_statistics.common_stats.CopyFrom(common_statistics)
      feature_name_statistics.bytes_stats.CopyFrom(bytes_statistics)

  return statistics, schema


def read_from_json(json_filename):
  """Read JSON-formatted proto into DatasetInfo proto."""
  with tf.io.gfile.GFile(json_filename) as f:
    dataset_info_json_str = f.read()
  # Parse it back into a proto.
  parsed_proto = json_format.Parse(dataset_info_json_str,
                                   dataset_info_pb2.DatasetInfo())
  return parsed_proto
