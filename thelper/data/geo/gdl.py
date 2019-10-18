"""Data parsers & utilities for cross-framework compatibility with Geo Deep Learning (GDL).

Geo Deep Learning (GDL) is a machine learning framework initiative for geospatial projects
lead by the wonderful folks at NRCan's CCMEO. See https://github.com/NRCan/geo-deep-learning
for more information.

The classes and functions defined here were used for the exploration of research topics and
for the validation and testing of new software components.
"""

import collections
import os

import h5py
import numpy as np

import thelper.data


class SegmentationDataset(thelper.data.parsers.SegmentationDataset):
    """Semantic segmentation dataset interface for GDL-based HDF5 parsing."""

    def __init__(self, class_names, work_folder, dataset_type, max_sample_count=None,
                 dontcare=None, transforms=None):
        super().__init__(class_names=class_names, input_key="sat_img", label_map_key="map_img",
                         meta_keys=["metadata"], dontcare=dontcare, transforms=transforms)
        # note: if 'max_sample_count' is None, then it will be read from the dataset at runtime
        self.work_folder = work_folder
        self.max_sample_count = max_sample_count
        self.dataset_type = dataset_type
        self.metadata = []
        self.hdf5_path = os.path.join(self.work_folder, self.dataset_type + "_samples.hdf5")
        with h5py.File(self.hdf5_path, "r") as hdf5_file:
            if "metadata" in hdf5_file:
                for i in range(hdf5_file["metadata"].shape[0]):
                    metadata = hdf5_file["metadata"][i, ...]
                    if isinstance(metadata, np.ndarray) and len(metadata) == 1:
                        metadata = metadata[0]
                    if isinstance(metadata, str):
                        if "ordereddict" in metadata:
                            metadata = metadata.replace("ordereddict", "collections.OrderedDict")
                        if metadata.startswith("collections.OrderedDict"):
                            metadata = eval(metadata)
                    self.metadata.append(metadata)
            if self.max_sample_count is None:
                self.max_sample_count = hdf5_file["sat_img"].shape[0]

    def __len__(self):
        return self.max_sample_count

    def __getitem__(self, index):
        with h5py.File(self.hdf5_path, "r") as hdf5_file:
            sat_img = hdf5_file["sat_img"][index, ...]
            map_img = hdf5_file["map_img"][index, ...]
            meta_idx = int(hdf5_file["meta_idx"][index]) if "meta_idx" in hdf5_file else -1
            metadata = None
            if meta_idx != -1:
                metadata = self.metadata[meta_idx]
        sample = {"sat_img": sat_img, "map_img": map_img, "metadata": metadata}
        if self.transforms:
            sample = self.transforms(sample)
        return sample


class MetaSegmentationDataset(SegmentationDataset):
    """Semantic segmentation dataset interface that appends metadata under new tensor layers."""

    metadata_handling_modes = ["const_prefix_channel", "const_postfix_channel"]  # TODO: add more

    def __init__(self, class_names, work_folder, dataset_type, meta_map, max_sample_count=None,
                 dontcare=None, transforms=None):
        assert isinstance(meta_map, dict), "unexpected metadata mapping object type"
        assert all([isinstance(k, str) and v in self.metadata_handling_modes for k, v in meta_map.items()]), \
            "unexpected metadata key type or value handling mode"
        super().__init__(class_names=class_names, work_folder=work_folder, dataset_type=dataset_type,
                         max_sample_count=max_sample_count, dontcare=dontcare, transforms=transforms)
        assert all([isinstance(m, (dict, collections.OrderedDict)) for m in self.metadata]), \
            "cannot use provided metadata object type with meta-mapping dataset interface"
        self.meta_map = meta_map

    @staticmethod
    def get_meta_value(map, key):
        if not isinstance(key, list):
            key = key.split("/")  # subdict indexing split using slash
        assert key[0] in map, f"missing key '{key[0]}' in metadata dictionary"
        val = map[key[0]]
        if isinstance(val, (dict, collections.OrderedDict)):
            assert len(key) > 1, "missing keys to index metadata subdictionaries"
            return MetaSegmentationDataset.get_meta_value(val, key[1:])
        return val

    def __getitem__(self, index):
        with h5py.File(self.hdf5_path, "r") as hdf5_file:
            sat_img = hdf5_file["sat_img"][index, ...]
            map_img = hdf5_file["map_img"][index, ...]
            meta_idx = int(hdf5_file["meta_idx"][index]) if "meta_idx" in hdf5_file else -1
            assert meta_idx != -1, f"metadata unvailable in sample #{index}"
            metadata = self.metadata[meta_idx]
            assert isinstance(metadata, (dict, collections.OrderedDict)), "unexpected metadata type"
        for meta_key, mode in self.meta_map.items():
            meta_val = self.get_meta_value(metadata, meta_key)
            if mode == "const_prefix_channel" or mode == "const_postfix_channel":
                assert np.isscalar(meta_val), "constant channel-wise assignment requires scalar value"
                layer = np.full(sat_img.shape[0:2], meta_val, dtype=np.float32)
                layer_idx = 0 if mode == "const_prefix_channel" else sat_img.shape[2]
                sat_img = np.insert(sat_img, layer_idx, layer, axis=2)
            #else...
        sample = {"sat_img": sat_img, "map_img": map_img}
        if self.transforms:
            sample = self.transforms(sample)
        return sample
