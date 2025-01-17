# =================================================================
# Copyright (C) 2021-2021 52°North Spatial Information Research GmbH
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published
# by the Free Software Foundation.
#
# If the program is linked with libraries which are licensed under one of
# the following licenses, the combination of the program with the linked
# library is not considered a "derivative work" of the program:
#
#     - Apache License, version 2.0
#     - Apache Software License, version 1.0
#     - GNU Lesser General Public License, version 3
#     - Mozilla Public License, versions 1.0, 1.1 and 2.0
#     - Common Development and Distribution License (CDDL), version 1.0
#
# Therefore the distribution of the program linked with libraries licensed
# under the aforementioned licenses, is permitted by the copyright holders
# if the distribution is compliant with both the GNU General Public
# License version 2 and the aforementioned licenses.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General
# Public License for more details.
#
# Based on pygeoapi's RasterioProvider (https://github.com/geopython/pygeoapi/blob/master/pygeoapi/provider/rasterio_.py)
#
# =================================================================


from pygeoapi.provider.base import (BaseProvider, ProviderConnectionError,
                                    ProviderQueryError)

from pandas import isnull
import numpy as np
from pyproj import CRS, Transformer
from rasterio.io import MemoryFile
from rasterio import Affine

from datacube.utils.geometry import bbox_union
from datacube.utils.geometry import CRS as CRS_dc
import datacube
import os

import logging

LOGGER = logging.getLogger(__name__)

ODC_CONFIG_FILE_DEFAULT="datacube.conf"

class OpenDataCubeProvider(BaseProvider):
    """OpenDataCube Provider
    This provider plugin maps an OGC collection to an ODC product
    """

    def __init__(self, provider_def):
        """
        Initialize object
        :param provider_def: provider definition
        :returns: pygeoapi.provider.rasterio_.RasterioProvider
        """

        super().__init__(provider_def)

        dc_config_file = os.environ.get("ODC_CONFIG_FILE")
        if dc_config_file is None:
            dc_config_file = ODC_CONFIG_FILE_DEFAULT

        self.dc = datacube.Datacube(config=dc_config_file)
        self.product_name = provider_def['product']

        try:
            self.crs_obj = None  # datacube.utils.geometry.CRS
            self._coverage_properties = self._get_coverage_properties()
            self._measurement_properties = self._get_measurement_properties()
            self.crs = self._coverage_properties['crs_uri']
            self.axes = self._coverage_properties['axes']
            self.num_bands = self._coverage_properties['num_bands']
            self.fields = [str(num) for num in range(1, self.num_bands + 1)]
            self.native_format = provider_def['format']['name']
        except Exception as err:
            LOGGER.warning(err)
            raise ProviderConnectionError(err)

    def query(self, range_subset=[], subsets={}, bbox=[], datetime_=None,
              format_='json', **kwargs):
        """
        Extract data from collection
        :param range_subset: list of bands
        :param subsets: dict of subset names with lists of ranges
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param datetime_: temporal (datestamp or extent)
        :param format_: data format of output

        :returns: coverage data as dict of CoverageJSON or native format
        """
        # ---------------- #
        # Query parameters (https://ogcapi.ogc.org/coverages/overview.html)
        # url: {datasetAPI}/collections/{coverageid}/coverage
        # Subset with well-defined ranges for named axes
        # ?subset=Lat(40:50),Lon(10: 20)
        # ?subset=time("2019-03-27")
        # Band subset
        # ?rangeSubset=B02,B03,B04
        # Bbox (in WGS84 or WGS84h)
        # ?bbox=10,40,20,50
        # Scaling
        # ?scaleSize=Lon(800),Lat(400)
        # ?scaleFactor=2
        # ?scaleAxes=Lon(2)
        # ---------------- #

        bands = range_subset
        LOGGER.debug('Bands: {}, subsets: {}'.format(bands, subsets))

        # initial bbox, full extent of collection
        minx, miny, maxx, maxy = self._coverage_properties['bbox']

        if all([not bands, not subsets, not bbox]):
            LOGGER.debug('No parameters specified')

        if all([self._coverage_properties['x_axis_label'] in subsets,
                self._coverage_properties['y_axis_label'] in subsets,
                len(bbox) > 0]):
            msg = 'bbox and subsetting by coordinates are exclusive'
            LOGGER.warning(msg)
            raise ProviderQueryError(msg)

        # -------------- #
        # Spatial subset #
        # -------------- #
        if len(bbox) > 0:
            minxbox, minybox, maxxbox, maxybox = bbox

            crs_src = CRS.from_epsg(4326)   # fixed by specification
            crs_dest = CRS.from_epsg(self.crs_obj.to_epsg())

            if crs_src == crs_dest:
                LOGGER.debug('source bbox CRS and data CRS are the same')
            else:
                LOGGER.debug('source bbox CRS and data CRS are different')
                LOGGER.debug('reprojecting bbox into native coordinates')

                t = Transformer.from_crs(crs_src, crs_dest, always_xy=True)
                minx, miny = t.transform(minxbox, minybox)
                maxx, maxy = t.transform(maxxbox, maxybox)

                LOGGER.debug('Source coordinates: {}'.format(
                    [minxbox, minybox, maxxbox, maxybox]))
                LOGGER.debug('Destination coordinates: {}'.format(
                    [minx, miny, maxx, maxy]))

        elif (self._coverage_properties['x_axis_label'] in subsets and
                self._coverage_properties['y_axis_label'] in subsets):
            LOGGER.debug('Creating spatial subset')

            x = self._coverage_properties['x_axis_label']
            y = self._coverage_properties['y_axis_label']

            minx = subsets[x][0]
            maxx = subsets[x][1]
            miny = subsets[y][0]
            maxy = subsets[y][1]

        # ------------------------------ #
        # Parameters for datacube.load() #
        # ------------------------------ #

        # Note: resolution and align expect the following coordinate order: (y, x)
        # Note: datacube.Datacube.load accepts all of the following parameters for spatial subsets
        #       independent of the crs: 'latitude' or 'lat' or 'y' / 'longitude' or 'lon' or 'long' or 'x'
        params = {
            "crs": "epsg:{}".format(self.crs_obj.to_epsg()),
            "align": (abs(self._coverage_properties['resy'] / 2),
                      abs(self._coverage_properties['resx'] / 2)),
            # in the units of output_crs (if output_crs is not supplied, crs of stored data is used)
            'x': (minx, maxx),
            'y': (miny, maxy),
        }

        if len(bands) > 0:
            params['measurements'] = bands

        # ToDo: enable output in different crs? Does API Coverages support this?
        # ToDo: check if reprojection is necessary
        reproj = False
        if reproj:
            params['resolution'] = (self._coverage_properties['resy'], self._coverage_properties['resx'])
            params['output_crs'] = "epsg:{}".format(self.crs_obj.to_epsg())
            # params['resampling'] = 'nearest'

        # ---------------------- #
        # Load data via datacube #
        # ---------------------- #
        dataset = self.dc.load(product=self.product_name, **params)

        # Use 'dataset.time.attrs.pop('units', None)' to prevent the following error:
        # "ValueError: failed to prevent overwriting existing key units in attrs on variable 'time'.
        # This is probably an encoding field used by xarray to describe how a variable is serialized.
        # To proceed, remove this key from the variable's attributes manually."
        dataset.time.attrs.pop('units', None)

        # ------------------------------------- #
        # Return coverage json or native format #
        # ------------------------------------- #
        if len(bands) == 0:
            bands = list(dataset.keys())   # select all bands

        out_meta = {'bbox': [minx, miny, maxx, maxy],
                    'width': abs((maxx - minx) / self._coverage_properties['resx']),
                    'height': abs((maxy - miny) / self._coverage_properties['resy']),
                    'bands': bands}

        if self.options is not None:
            LOGGER.debug('Adding dataset options')
            for key, value in self.options.items():
                out_meta[key] = value

        if format_ == 'json':
            LOGGER.debug('Creating output in CoverageJSON')
            return self.gen_covjson(out_meta, dataset)

        elif format_.lower() == 'geotiff':
            # ToDo: check if there is more than one time slice
            out_meta['driver'] = 'GTiff'
            out_meta['crs'] = self.crs_obj.to_epsg()
            out_meta['dtype'] = self._measurement_properties[0]['dtype']
            out_meta['nodata'] = self._measurement_properties[0]['nodata']
            out_meta['count'] = len(bands)
            # ToDO: Coordinates seem to be shifted by resolution/2
            out_meta['transform'] = Affine(self._coverage_properties['transform'][0],
                                           self._coverage_properties['transform'][1],
                                           self._coverage_properties['transform'][2],
                                           self._coverage_properties['transform'][3],
                                           self._coverage_properties['transform'][4],
                                           self._coverage_properties['transform'][5])

            with MemoryFile() as memfile:
                with memfile.open(**out_meta) as dest:
                    dest.write(np.stack([dataset.squeeze(dim='time', drop=True)[bs].values for bs in bands], axis=0))   # input is expected as (bands, rows, cols)

                LOGGER.debug('Returning data as GeoTIFF')
                return memfile.read()

        else:
            LOGGER.debug('Returning data as netCDF')
            # Note: "If no path is provided, this function returns the resulting netCDF file as bytes; in this case,
            # we need to use scipy, which does not support netCDF version 4 (the default format becomes NETCDF3_64BIT)."
            # (http://xarray.pydata.org/en/stable/generated/xarray.Dataset.to_netcdf.html)
            return dataset.to_netcdf()

    def gen_covjson(self, metadata, dataset):
        """
        Generate coverage as CoverageJSON representation
        :param metadata: coverage metadata
        :param dataset: xarray Dataset object
        :returns: dict of CoverageJSON representation
        """

        # ToDo: support time dimension

        LOGGER.debug('Creating CoverageJSON domain')
        minx, miny, maxx, maxy = metadata['bbox']

        cj = {
            'type': 'Coverage',
            'domain': {
                'type': 'Domain',
                'domainType': 'Grid',
                'axes': {
                    'x': {
                        'start': minx,
                        'stop': maxx,
                        'num': metadata['width']
                    },
                    'y': {
                        'start': miny,
                        'stop': maxy,
                        'num': metadata['height']
                    }
                },
                'referencing': [{
                    'coordinates': ['x', 'y'],
                    'system': {
                        'type': self._coverage_properties['crs_type'],
                        'id': self._coverage_properties['crs_uri']
                    }
                }]
            },
            'parameters': {},
            'ranges': {}
        }

        bands_select = metadata['bands']

        LOGGER.debug('bands selected: {}'.format(bands_select))
        for bs in bands_select:

            parameter = {
                'type': 'Parameter',
                'description': bs,
                'unit': {
                    'symbol': dataset[bs].attrs['units']
                },
                'observedProperty': {
                    'id': bs,
                    'label': {
                        'en': bs
                    }
                }
            }

            cj['parameters'][bs] = parameter

        # ToDo: check shape/axis order!
        try:
            for key in cj['parameters'].keys():
                cj['ranges'][key] = {
                    'type': 'NdArray',
                    'dataType': str(dataset[key].dtype),
                    'axisNames': ['y', 'x'],
                    'shape': [metadata['height'], metadata['width']],
                }
                cj['ranges'][key]['values'] = dataset[key].values.flatten().tolist()
        except IndexError as err:
            LOGGER.warning(err)
            raise ProviderQueryError('Invalid query parameter')

        return cj

    def get_coverage_domainset(self):
        """
         Provide coverage domainset
         :returns: CIS JSON object of domainset metadata
         """

        domainset = {
            'type': 'DomainSetType',
            'generalGrid': {
                'type': 'GeneralGridCoverageType',
                'srsName': self._coverage_properties['crs_uri'],
                'axisLabels': [
                    self._coverage_properties['x_axis_label'],
                    self._coverage_properties['y_axis_label']
                ],
                'axis': [{
                    'type': 'RegularAxisType',
                    'axisLabel': self._coverage_properties['x_axis_label'],
                    'lowerBound': self._coverage_properties['bbox'][0],
                    'upperBound': self._coverage_properties['bbox'][2],
                    'uomLabel': self._coverage_properties['bbox_units'],
                    'resolution': self._coverage_properties['resx']
                }, {
                    'type': 'RegularAxisType',
                    'axisLabel': self._coverage_properties['y_axis_label'],
                    'lowerBound': self._coverage_properties['bbox'][1],
                    'upperBound': self._coverage_properties['bbox'][3],
                    'uomLabel': self._coverage_properties['bbox_units'],
                    'resolution': self._coverage_properties['resy']
                }],
                'gridLimits': {
                    'type': 'GridLimitsType',
                    'srsName': 'http://www.opengis.net/def/crs/OGC/0/Index2D',
                    'axisLabels': ['i', 'j'],
                    'axis': [{
                        'type': 'IndexAxisType',
                        'axisLabel': 'i',
                        'lowerBound': 0,
                        'upperBound': self._coverage_properties['width']
                    }, {
                        'type': 'IndexAxisType',
                        'axisLabel': 'j',
                        'lowerBound': 0,
                        'upperBound': self._coverage_properties['height']
                    }]
                }
            },
            # '_meta': {
            #     'tags': self._coverage_properties['tags']
            # }
        }

        return domainset

    def get_coverage_rangetype(self):
        """
        Provide coverage rangetype
        :returns: CIS JSON object of rangetype metadata
        """

        fields = []
        for row in range(0, len(self._measurement_properties)):
            fields.append({
                "id": self._measurement_properties[row]['id'],
                "type": "QuantityType",
                "name": self._measurement_properties[row]['name'],
                "definition": self._measurement_properties[row]['dtype'],
                "nodata": self._measurement_properties[row]['nodata'],
                "uom": {
                    # "id": "http://www.opengis.net/def/uom/UCUM/[C]", # ToDo: get correct uri for arbitrary units
                    "type": "UnitReference",
                    "code": self._measurement_properties[row]['unit']
                },
                "_meta": {
                    "tags": {
                        "Aliases": self._measurement_properties[row]['aliases'],
                    }
                }
            })

        rangetype = {
            "type": "DataRecordType",
            "field": fields
        }

        return rangetype

    def _get_coverage_properties(self):
        """
        Helper function to normalize coverage properties
        :returns: `dict` of coverage properties
        """

        # Note:
        # - Some metadata are specified on product level and some on dataset level or even on both (e.g. crs)
        # - Some (product) metadata are optional, thus they may not be available for all products
        # - Different datasets for the same product may have different crs

        # ---------------- #
        # Product metadata #
        # ---------------- #
        # With datacube versions > 1.8.3, metadata can alternatively be obtained like this:
        # self.dc.index.products.get_by_name(self.product_name).default_crs
        # https://datacube-core.readthedocs.io/en/latest/dev/api/generate/datacube.model.DatasetType.html#datacube.model.DatasetType

        product_list = self.dc.list_products()
        product_metadata = product_list[product_list['name'] == self.product_name]

        res = product_metadata.iloc[0]['resolution']
        if isinstance(res, tuple):
            # ToDO: check coordinate order!
            resx = res[1]
            resy = res[0]
        else:
            resx = None
            resy = None

        crs_str = product_metadata.iloc[0]['crs']

        # spatial_dimensions = product_metadata.iloc[0]['spatial_dimensions']
        # if isinstance(spatial_dimensions, tuple):
        #     # ToDO: check axis order!
        #     dim_we = spatial_dimensions[1]
        #     dim_ns = spatial_dimensions[0]
        # else:
        #     dim_we = None
        #     dim_ns = None

        num_bands = len(self.dc.index.products.get_by_name(self.product_name).measurements)

        # ---------------- #
        # Dataset metadata #
        # ---------------- #
        bbs = []
        crs_list = []
        resx_list = []
        resy_list = []
        transform_list = []
        dim_list = []
        for dataset in self.dc.find_datasets(product=self.product_name):
            bbs.append(dataset.bounds)
            crs_list.append(dataset.crs)
            # ToDo: check coordinate order!
            resx_list.append(dataset.__dict__['metadata_doc']['grids']['default']['transform'][0])
            resy_list.append(dataset.__dict__['metadata_doc']['grids']['default']['transform'][4])
            transform_list.append(dataset.__dict__['metadata_doc']['grids']['default']['transform'])
            dim_list.append(dataset.crs.dimensions)

        # Check if all datasets have the same crs, resolution and transform
        if len(set(crs_list)) > 1:
            LOGGER.warning("Product {} has datasets with different coordinate reference systems.".format(self.product_name))
        if len(set(resx_list)) > 1 or len(set(resy_list)) > 1:
            LOGGER.warning("Product {} has datasets with different spatial resolutions.".format(self.product_name))
        if len(set(tuple(i) for i in transform_list)) > 1:
            LOGGER.warning("Product {} has datasets with different transforms.".format(self.product_name))

        bounds = bbox_union(bbs)

        # Use dataset metadata if metadata was not specified on product level
        # ToDO: support different crs/resolution for different datasets including reprojection
        if crs_str is None or isnull(crs_str):
            self.crs_obj = crs_list[0]
        else:
            self.crs_obj = CRS_dc(crs_str)

        if resx is None:
            resx = resx_list[0]
        if resy is None:
            resy = resy_list[0]

        transform = transform_list[0]

        # if dim_we is None:
        #     dim_we = dim_list[1]
        # if dim_ns is None:
        #     dim_ns = dim_list[0]

        # -------------- #
        # Set properties #
        # -------------- #
        properties = {
            'bbox': [
                bounds.left,
                bounds.bottom,
                bounds.right,
                bounds.top
            ],
            'crs_uri': 'http://www.opengis.net/def/crs/OGC/1.3/CRS84',
            'crs_type': 'GeographicCRS',
            'bbox_units': 'deg',
            'x_axis_label': 'Lon',
            'y_axis_label': 'Lat',
            'width': abs((bounds.right - bounds.left)/resx),
            'height': abs((bounds.top - bounds.bottom)/resy),
            'resx': resx,
            'resy': resy,
            'transform': transform,
            'num_bands': num_bands,
            #'tags': 'tags'
        }

        if self.crs_obj.projected:
            properties['crs_uri'] = '{}/{}'.format('http://www.opengis.net/def/crs/EPSG/9.8.15', self.crs_obj.to_epsg())
            properties['x_axis_label'] = 'x'
            properties['y_axis_label'] = 'y'
            properties['bbox_units'] = self.crs_obj.units[0]
            properties['crs_type'] = 'ProjectedCRS'

        properties['axes'] = [
            properties['x_axis_label'], properties['y_axis_label']
        ]

        return properties

    def _get_measurement_properties(self):
        """
        Helper function to normalize measurement properties
        :returns: `dict` of measurement properties
        """

        measurement_list = self.dc.list_measurements()
        measurement_metadata = measurement_list.loc[self.product_name]

        properties = []
        for row in range(0, len(measurement_metadata)):
            properties.append({
                "id": row + 1,
                "name": measurement_metadata.iloc[row]['name'],
                "dtype": measurement_metadata.iloc[row]['dtype'],
                "nodata": measurement_metadata.iloc[row]['nodata'],
                "unit": measurement_metadata.iloc[row]['units'],
                "aliases": measurement_metadata.iloc[row]['aliases'] if 'aliases' in measurement_metadata.columns else None,
            })

        return properties
