"""Data parsers & utilities module for OGC-related projects."""

import functools
import logging

import cv2 as cv
import numpy as np
import tqdm

import thelper.data
import thelper.data.geo as geo

logger = logging.getLogger(__name__)


class TB15D104Dataset(geo.parsers.VectorCropDataset):
    """OGC-Testbed 15 dataset parser for D104 (lake/river) segmentation task."""

    TYPECE_RIVER = "10"
    TYPECE_LAKE = "21"

    BACKGROUND_ID = 0
    LAKE_ID = 1

    def __init__(self, raster_path, vector_path, px_size=None,
                 allow_outlying_vectors=True, clip_outlying_vectors=True,
                 lake_area_min=0.0, lake_area_max=float("inf"),
                 lake_river_max_dist=float("inf"), roi_buffer=1000,
                 focus_lakes=True, srs_target="3857", force_parse=False,
                 reproj_rasters=False, reproj_all_cpus=True, display_debug=False,
                 keep_rasters_open=True, parallel=False, transforms=None):
        assert isinstance(lake_river_max_dist, (float, int)) and lake_river_max_dist >= 0, "unexpected dist type"
        self.lake_river_max_dist = float(lake_river_max_dist)
        assert isinstance(focus_lakes, bool), "unexpected flag type"
        self.focus_lakes = focus_lakes
        assert px_size is None or isinstance(px_size, (float, int)), "pixel size (resolution) must be float/int"
        px_size = (1.0, 1.0) if px_size is None else (float(px_size), float(px_size))
        # note: we wrap partial static functions for caching to see when internal parameters are changing
        cleaner = functools.partial(self._feature_cleaner, area_min=lake_area_min, area_max=lake_area_max,
                                    lake_river_max_dist=lake_river_max_dist, parallel=parallel)
        if self.focus_lakes:
            cropper = functools.partial(self._lake_cropper, px_size=px_size, skew=(0.0, 0.0),
                                        roi_buffer=roi_buffer, parallel=parallel)
        else:
            # TODO: implement river-focused cropper (i.e. river-length parsing?)
            raise NotImplementedError
        super().__init__(raster_path=raster_path, vector_path=vector_path, px_size=px_size, skew=None,
                         allow_outlying_vectors=allow_outlying_vectors, clip_outlying_vectors=clip_outlying_vectors,
                         vector_area_min=lake_area_min, vector_area_max=lake_area_max,
                         vector_target_prop=None, vector_roi_buffer=roi_buffer, srs_target=srs_target,
                         raster_key="lidar", mask_key="hydro", cleaner=cleaner, cropper=cropper,
                         force_parse=force_parse, reproj_rasters=reproj_rasters, reproj_all_cpus=reproj_all_cpus,
                         keep_rasters_open=keep_rasters_open, transforms=transforms)
        meta_keys = self.task.meta_keys
        if "bboxes" in meta_keys:
            del meta_keys[meta_keys.index("bboxes")]  # placed in meta list by base class constr, moved to detect target below
        self.task = thelper.tasks.Detection(class_names={"background": self.BACKGROUND_ID, "lake": self.LAKE_ID},
                                            input_key="input", bboxes_key="bboxes",
                                            meta_keys=meta_keys, background=0)
        # update all already-created bboxes with new task ref
        for s in self.samples:
            for b in s["bboxes"]:
                b.task = self.task
        self.display_debug = display_debug
        self.parallel = parallel

    @staticmethod
    def _feature_cleaner(features, area_min, area_max, lake_river_max_dist, parallel=False):
        """Flags geometric features as 'clean' based on type and distance to nearest river."""
        # note: we use a flag here instead of removing bad features so that end-users can still use them if needed
        for f in features:
            f["clean"] = False  # flag every as 'bad' by default, clear just the ones of interest below
        rivers = [f for f in features if f["properties"]["TYPECE"] == TB15D104Dataset.TYPECE_RIVER]
        lakes = [f for f in features if f["properties"]["TYPECE"] == TB15D104Dataset.TYPECE_LAKE]
        logger.info(f"labeling and cleaning {len(lakes)} lakes...")

        def clean_lake(lake):
            if area_min <= lake["geometry"].area <= area_max:
                if lake_river_max_dist == float("inf"):
                    return True
                else:
                    for river in rivers:
                        # note: distance check below seems to be "intelligent", i.e. it will
                        # first check bbox distance, then check chull distance, and finally use
                        # the full geometries (doing these steps here explicitly makes it slower)
                        if lake["geometry"].distance(river["geometry"]) < lake_river_max_dist:
                            return True
            return False

        if parallel:
            if not isinstance(parallel, int):
                import multiprocessing
                parallel = multiprocessing.cpu_count()
            assert parallel > 0, "unexpected min core count"
            import joblib
            flags = joblib.Parallel(n_jobs=parallel)(joblib.delayed(
                clean_lake)(lake) for lake in tqdm.tqdm(lakes, desc="labeling + cleaning lakes"))
            for flag, lake in zip(flags, lakes):
                lake["clean"] = flag
        else:
            for lake in tqdm.tqdm(lakes, desc="labeling + cleaning lakes"):
                lake["clean"] = clean_lake(lake)
        return features

    @staticmethod
    def _lake_cropper(features, rasters_data, px_size, skew, roi_buffer, parallel=False):
        """Returns the ROI information for a given feature (may be modified in derived classes)."""

        def crop_feature(feature):
            if not feature["clean"]:
                return None  # skip (will not use bad features as the origin of a 'sample')
            roi, roi_tl, roi_br, crop_width, crop_height = \
                geo.utils.get_feature_roi(feature["geometry"], px_size, skew, roi_buffer)
            roi_geotransform = (roi_tl[0], px_size[0], skew[0],
                                roi_tl[1], skew[1], px_size[1])
            # test all raster regions that touch the selected feature
            roi_hits = []
            for raster_idx, raster_data in enumerate(rasters_data):
                if raster_data["target_roi"].intersects(roi):
                    roi_hits.append(raster_idx)
            # make list of all other features that may be included in the roi
            roi_centroid = feature["centroid"]
            roi_radius = np.linalg.norm(np.asarray(roi_tl) - np.asarray(roi_br)) / 2
            roi_features, bboxes = [], []
            # note: the 'image id' is in fact the id of the focal feature in the crop
            image_id = int(feature["properties"]["OBJECTID"])
            for f in features:
                if f["centroid"].distance(roi_centroid) > roi_radius:
                    continue
                inters = f["geometry"].intersection(roi)
                if inters.is_empty:
                    continue
                roi_features.append(f)
                if f["properties"]["TYPECE"] == TB15D104Dataset.TYPECE_RIVER:
                    continue
                # only lakes can generate bboxes; make sure to clip them to the roi bounds
                clip = f["clipped"] or not inters.equals(f["geometry"])
                if clip:
                    assert inters.geom_type in ["Polygon", "MultiPolygon"], "unexpected inters type"
                    corners = []
                    if inters.geom_type == "Polygon":
                        bounds = inters.bounds
                        corners.append((bounds[0:2], bounds[2:4]))
                    elif inters.geom_type == "MultiPolygon":
                        for poly in inters:
                            bounds = poly.bounds
                            corners.append((bounds[0:2], bounds[2:4]))
                else:
                    corners = [(f["tl"], f["br"])]
                for c in corners:
                    feat_tl_px = geo.utils.get_pxcoord(roi_geotransform, *c[0])
                    feat_br_px = geo.utils.get_pxcoord(roi_geotransform, *c[1])
                    bbox = [max(0, feat_tl_px[0]), max(0, feat_tl_px[1]),
                            min(crop_width - 1, feat_br_px[0]),
                            min(crop_height - 1, feat_br_px[1])]
                    # note: lake class id is 1 by definition
                    bboxes.append(thelper.tasks.detect.BoundingBox(TB15D104Dataset.LAKE_ID,
                                                                   bbox=bbox,
                                                                   include_margin=False,
                                                                   truncated=clip,
                                                                   image_id=image_id))
            # prepare actual 'sample' for crop generation at runtime
            return {
                "features": roi_features,
                "bboxes": bboxes,
                "focal": feature,
                "id": image_id,
                "roi": roi,
                "roi_tl": roi_tl,
                "roi_br": roi_br,
                "roi_hits": roi_hits,
                "crop_width": crop_width,
                "crop_height": crop_height,
                "geotransform": roi_geotransform,
            }

        if parallel:
            if not isinstance(parallel, int):
                import multiprocessing
                parallel = multiprocessing.cpu_count()
            assert parallel > 0, "unexpected min core count"
            import joblib
            samples = joblib.Parallel(n_jobs=parallel)(joblib.delayed(
                crop_feature)(feat) for feat in tqdm.tqdm(features, desc="preparing crop regions"))
        else:
            samples = []
            for feature in tqdm.tqdm(features, desc="preparing crop regions"):
                samples.append(crop_feature(feature))
        return [s for s in samples if s is not None]

    def _show_stats_plots(self, show=False, block=False):
        """Draws and returns feature stats histograms using pyplot."""
        import matplotlib.pyplot as plt
        feature_categories = {}
        for feat in self.features:
            curr_cat = feat["properties"]["TYPECE"]
            if curr_cat not in feature_categories:
                feature_categories[curr_cat] = []
            feature_categories[curr_cat].append(feat)
        fig, axes = plt.subplots(len(feature_categories))
        for idx, (cat, features) in enumerate(feature_categories.items()):
            areas = [f["geometry"].area for f in features]
            axes[idx].hist(areas, density=True, bins=30,
                           range=(max(self.area_min, min(areas)), min(self.area_max, max(areas))))
            axes[idx].set_xlabel("Surface (m^2)")
            axes[idx].set_title(f"TYPECE = {cat}")
            axes[idx].set_xlim(xmin=0)
        if show:
            fig.show()
            if block:
                plt.show(block=block)
                return fig
            plt.pause(0.5)
        return fig, axes

    def __getitem__(self, idx):
        """Returns the data sample (a dictionary) for a specific (0-based) index."""
        if isinstance(idx, slice):
            return self._getitems(idx)
        assert idx < len(self.samples), "sample index is out-of-range"
        if idx < 0:
            idx = len(self.samples) + idx
        sample = self.samples[idx]
        crop, mask = self._process_crop(sample)
        assert crop.shape[2] == 1, "unexpected lidar raster band count"
        crop = crop[:, :, 0]
        dmap = cv.distanceTransform(np.where(mask, np.uint8(0), np.uint8(255)), cv.DIST_L2, cv.DIST_MASK_PRECISE)
        dmap_inv = cv.distanceTransform(np.where(mask, np.uint8(255), np.uint8(0)), cv.DIST_L2, cv.DIST_MASK_PRECISE)
        dmap = np.where(mask, -dmap_inv, dmap)
        dmap *= self.px_size[0]  # constructor enforces same px width/height size
        # dc mask is crop.mask, but most likely lost below
        if self.display_debug:
            crop = cv.normalize(crop, dst=crop, alpha=0, beta=255, norm_type=cv.NORM_MINMAX,
                                dtype=cv.CV_8U, mask=(~crop.mask).astype(np.uint8))
            mask = cv.normalize(mask, dst=mask, alpha=0, beta=255, norm_type=cv.NORM_MINMAX, dtype=cv.CV_8U)
            dmap = cv.normalize(dmap, dst=dmap, alpha=0, beta=255, norm_type=cv.NORM_MINMAX, dtype=cv.CV_8U)
        sample = {
            "input": np.stack([crop, mask, dmap], axis=-1),
            # note: bboxes are automatically added in the "cropper" preprocessing function
            "hydro": mask,
            **sample
        }
        if self.transforms:
            sample = self.transforms(sample)
        return sample
