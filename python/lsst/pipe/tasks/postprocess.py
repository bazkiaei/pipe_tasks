# This file is part of pipe_tasks.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

__all__ = ["WriteObjectTableConfig", "WriteObjectTableTask",
           "WriteSourceTableConfig", "WriteSourceTableTask",
           "WriteRecalibratedSourceTableConfig", "WriteRecalibratedSourceTableTask",
           "PostprocessAnalysis",
           "TransformCatalogBaseConfig", "TransformCatalogBaseTask",
           "TransformObjectCatalogConfig", "TransformObjectCatalogTask",
           "ConsolidateObjectTableConfig", "ConsolidateObjectTableTask",
           "TransformSourceTableConfig", "TransformSourceTableTask",
           "ConsolidateVisitSummaryConfig", "ConsolidateVisitSummaryTask",
           "ConsolidateSourceTableConfig", "ConsolidateSourceTableTask",
           "MakeCcdVisitTableConfig", "MakeCcdVisitTableTask",
           "MakeVisitTableConfig", "MakeVisitTableTask",
           "WriteForcedSourceTableConfig", "WriteForcedSourceTableTask",
           "TransformForcedSourceTableConfig", "TransformForcedSourceTableTask",
           "ConsolidateTractConfig", "ConsolidateTractTask"]

import functools
import pandas as pd
import logging
import numpy as np
import numbers
import os

import lsst.geom
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
import lsst.daf.base as dafBase
from lsst.pipe.base import connectionTypes
import lsst.afw.table as afwTable
from lsst.afw.image import ExposureSummaryStats
from lsst.meas.base import SingleFrameMeasurementTask, DetectorVisitIdGeneratorConfig
from lsst.skymap import BaseSkyMap

from .functors import CompositeFunctor, Column

log = logging.getLogger(__name__)


def flattenFilters(df, noDupCols=['coord_ra', 'coord_dec'], camelCase=False, inputBands=None):
    """Flattens a dataframe with multilevel column index.
    """
    newDf = pd.DataFrame()
    # band is the level 0 index
    dfBands = df.columns.unique(level=0).values
    for band in dfBands:
        subdf = df[band]
        columnFormat = '{0}{1}' if camelCase else '{0}_{1}'
        newColumns = {c: columnFormat.format(band, c)
                      for c in subdf.columns if c not in noDupCols}
        cols = list(newColumns.keys())
        newDf = pd.concat([newDf, subdf[cols].rename(columns=newColumns)], axis=1)

    # Band must be present in the input and output or else column is all NaN:
    presentBands = dfBands if inputBands is None else list(set(inputBands).intersection(dfBands))
    # Get the unexploded columns from any present band's partition
    noDupDf = df[presentBands[0]][noDupCols]
    newDf = pd.concat([noDupDf, newDf], axis=1)
    return newDf


class WriteObjectTableConnections(pipeBase.PipelineTaskConnections,
                                  defaultTemplates={"coaddName": "deep"},
                                  dimensions=("tract", "patch", "skymap")):
    inputCatalogMeas = connectionTypes.Input(
        doc="Catalog of source measurements on the deepCoadd.",
        dimensions=("tract", "patch", "band", "skymap"),
        storageClass="SourceCatalog",
        name="{coaddName}Coadd_meas",
        multiple=True
    )
    inputCatalogForcedSrc = connectionTypes.Input(
        doc="Catalog of forced measurements (shape and position parameters held fixed) on the deepCoadd.",
        dimensions=("tract", "patch", "band", "skymap"),
        storageClass="SourceCatalog",
        name="{coaddName}Coadd_forced_src",
        multiple=True
    )
    inputCatalogRef = connectionTypes.Input(
        doc="Catalog marking the primary detection (which band provides a good shape and position)"
            "for each detection in deepCoadd_mergeDet.",
        dimensions=("tract", "patch", "skymap"),
        storageClass="SourceCatalog",
        name="{coaddName}Coadd_ref"
    )
    outputCatalog = connectionTypes.Output(
        doc="A vertical concatenation of the deepCoadd_{ref|meas|forced_src} catalogs, "
            "stored as a DataFrame with a multi-level column index per-patch.",
        dimensions=("tract", "patch", "skymap"),
        storageClass="DataFrame",
        name="{coaddName}Coadd_obj"
    )


class WriteObjectTableConfig(pipeBase.PipelineTaskConfig,
                             pipelineConnections=WriteObjectTableConnections):
    engine = pexConfig.Field(
        dtype=str,
        default="pyarrow",
        doc="Parquet engine for writing (pyarrow or fastparquet)",
        deprecated="This config is no longer used, and will be removed after v26."
    )
    coaddName = pexConfig.Field(
        dtype=str,
        default="deep",
        doc="Name of coadd"
    )


class WriteObjectTableTask(pipeBase.PipelineTask):
    """Write filter-merged source tables as a DataFrame in parquet format.
    """
    _DefaultName = "writeObjectTable"
    ConfigClass = WriteObjectTableConfig

    # Names of table datasets to be merged
    inputDatasets = ('forced_src', 'meas', 'ref')

    # Tag of output dataset written by `MergeSourcesTask.write`
    outputDataset = 'obj'

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)

        measDict = {ref.dataId['band']: {'meas': cat} for ref, cat in
                    zip(inputRefs.inputCatalogMeas, inputs['inputCatalogMeas'])}
        forcedSourceDict = {ref.dataId['band']: {'forced_src': cat} for ref, cat in
                            zip(inputRefs.inputCatalogForcedSrc, inputs['inputCatalogForcedSrc'])}

        catalogs = {}
        for band in measDict.keys():
            catalogs[band] = {'meas': measDict[band]['meas'],
                              'forced_src': forcedSourceDict[band]['forced_src'],
                              'ref': inputs['inputCatalogRef']}
        dataId = butlerQC.quantum.dataId
        df = self.run(catalogs=catalogs, tract=dataId['tract'], patch=dataId['patch'])
        outputs = pipeBase.Struct(outputCatalog=df)
        butlerQC.put(outputs, outputRefs)

    def run(self, catalogs, tract, patch):
        """Merge multiple catalogs.

        Parameters
        ----------
        catalogs : `dict`
            Mapping from filter names to dict of catalogs.
        tract : int
            tractId to use for the tractId column.
        patch : str
            patchId to use for the patchId column.

        Returns
        -------
        catalog : `pandas.DataFrame`
            Merged dataframe.
        """

        dfs = []
        for filt, tableDict in catalogs.items():
            for dataset, table in tableDict.items():
                # Convert afwTable to pandas DataFrame
                df = table.asAstropy().to_pandas().set_index('id', drop=True)

                # Sort columns by name, to ensure matching schema among patches
                df = df.reindex(sorted(df.columns), axis=1)
                df['tractId'] = tract
                df['patchId'] = patch

                # Make columns a 3-level MultiIndex
                df.columns = pd.MultiIndex.from_tuples([(dataset, filt, c) for c in df.columns],
                                                       names=('dataset', 'band', 'column'))
                dfs.append(df)

        catalog = functools.reduce(lambda d1, d2: d1.join(d2), dfs)
        return catalog


class WriteSourceTableConnections(pipeBase.PipelineTaskConnections,
                                  defaultTemplates={"catalogType": ""},
                                  dimensions=("instrument", "visit", "detector")):

    catalog = connectionTypes.Input(
        doc="Input full-depth catalog of sources produced by CalibrateTask",
        name="{catalogType}src",
        storageClass="SourceCatalog",
        dimensions=("instrument", "visit", "detector")
    )
    outputCatalog = connectionTypes.Output(
        doc="Catalog of sources, `src` in DataFrame/Parquet format. The 'id' column is "
            "replaced with an index; all other columns are unchanged.",
        name="{catalogType}source",
        storageClass="DataFrame",
        dimensions=("instrument", "visit", "detector")
    )


class WriteSourceTableConfig(pipeBase.PipelineTaskConfig,
                             pipelineConnections=WriteSourceTableConnections):
    idGenerator = DetectorVisitIdGeneratorConfig.make_field()


class WriteSourceTableTask(pipeBase.PipelineTask):
    """Write source table to DataFrame Parquet format.
    """
    _DefaultName = "writeSourceTable"
    ConfigClass = WriteSourceTableConfig

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)
        inputs['ccdVisitId'] = self.config.idGenerator.apply(butlerQC.quantum.dataId).catalog_id
        result = self.run(**inputs)
        outputs = pipeBase.Struct(outputCatalog=result.table)
        butlerQC.put(outputs, outputRefs)

    def run(self, catalog, ccdVisitId=None, **kwargs):
        """Convert `src` catalog to DataFrame

        Parameters
        ----------
        catalog: `afwTable.SourceCatalog`
            catalog to be converted
        ccdVisitId: `int`
            ccdVisitId to be added as a column
        **kwargs
            Additional keyword arguments are ignored as a convenience for
            subclasses that pass the same arguments to several different
            methods.

        Returns
        -------
        result : `lsst.pipe.base.Struct`
            ``table``
                `DataFrame` version of the input catalog
        """
        self.log.info("Generating DataFrame from src catalog ccdVisitId=%s", ccdVisitId)
        df = catalog.asAstropy().to_pandas().set_index('id', drop=True)
        df['ccdVisitId'] = ccdVisitId
        return pipeBase.Struct(table=df)


class WriteRecalibratedSourceTableConnections(WriteSourceTableConnections,
                                              defaultTemplates={"catalogType": "",
                                                                "skyWcsName": "gbdesAstrometricFit",
                                                                "photoCalibName": "fgcm"},
                                              dimensions=("instrument", "visit", "detector", "skymap")):
    skyMap = connectionTypes.Input(
        doc="skyMap needed to choose which tract-level calibrations to use when multiple available",
        name=BaseSkyMap.SKYMAP_DATASET_TYPE_NAME,
        storageClass="SkyMap",
        dimensions=("skymap",),
    )
    exposure = connectionTypes.Input(
        doc="Input exposure to perform photometry on.",
        name="calexp",
        storageClass="ExposureF",
        dimensions=["instrument", "visit", "detector"],
    )
    externalSkyWcsTractCatalog = connectionTypes.Input(
        doc=("Per-tract, per-visit wcs calibrations.  These catalogs use the detector "
             "id for the catalog id, sorted on id for fast lookup."),
        name="{skyWcsName}SkyWcsCatalog",
        storageClass="ExposureCatalog",
        dimensions=["instrument", "visit", "tract"],
        multiple=True
    )
    externalSkyWcsGlobalCatalog = connectionTypes.Input(
        doc=("Per-visit wcs calibrations computed globally (with no tract information). "
             "These catalogs use the detector id for the catalog id, sorted on id for "
             "fast lookup."),
        name="finalVisitSummary",
        storageClass="ExposureCatalog",
        dimensions=["instrument", "visit"],
    )
    externalPhotoCalibTractCatalog = connectionTypes.Input(
        doc=("Per-tract, per-visit photometric calibrations.  These catalogs use the "
             "detector id for the catalog id, sorted on id for fast lookup."),
        name="{photoCalibName}PhotoCalibCatalog",
        storageClass="ExposureCatalog",
        dimensions=["instrument", "visit", "tract"],
        multiple=True
    )
    externalPhotoCalibGlobalCatalog = connectionTypes.Input(
        doc=("Per-visit photometric calibrations computed globally (with no tract "
             "information).  These catalogs use the detector id for the catalog id, "
             "sorted on id for fast lookup."),
        name="finalVisitSummary",
        storageClass="ExposureCatalog",
        dimensions=["instrument", "visit"],
    )

    def __init__(self, *, config=None):
        super().__init__(config=config)
        # Same connection boilerplate as all other applications of
        # Global/Tract calibrations
        if config.doApplyExternalSkyWcs and config.doReevaluateSkyWcs:
            if config.useGlobalExternalSkyWcs:
                self.inputs.remove("externalSkyWcsTractCatalog")
            else:
                self.inputs.remove("externalSkyWcsGlobalCatalog")
        else:
            self.inputs.remove("externalSkyWcsTractCatalog")
            self.inputs.remove("externalSkyWcsGlobalCatalog")
        if config.doApplyExternalPhotoCalib and config.doReevaluatePhotoCalib:
            if config.useGlobalExternalPhotoCalib:
                self.inputs.remove("externalPhotoCalibTractCatalog")
            else:
                self.inputs.remove("externalPhotoCalibGlobalCatalog")
        else:
            self.inputs.remove("externalPhotoCalibTractCatalog")
            self.inputs.remove("externalPhotoCalibGlobalCatalog")


class WriteRecalibratedSourceTableConfig(WriteSourceTableConfig,
                                         pipelineConnections=WriteRecalibratedSourceTableConnections):

    doReevaluatePhotoCalib = pexConfig.Field(
        dtype=bool,
        default=True,
        doc=("Add or replace local photoCalib columns")
    )
    doReevaluateSkyWcs = pexConfig.Field(
        dtype=bool,
        default=True,
        doc=("Add or replace local WCS columns and update the coord columns, coord_ra and coord_dec")
    )
    doApplyExternalPhotoCalib = pexConfig.Field(
        dtype=bool,
        default=True,
        doc=("If and only if doReevaluatePhotoCalib, apply the photometric calibrations from an external ",
             "algorithm such as FGCM or jointcal, else use the photoCalib already attached to the exposure."),
    )
    doApplyExternalSkyWcs = pexConfig.Field(
        dtype=bool,
        default=True,
        doc=("if and only if doReevaluateSkyWcs, apply the WCS from an external algorithm such as jointcal, ",
             "else use the wcs already attached to the exposure."),
    )
    useGlobalExternalPhotoCalib = pexConfig.Field(
        dtype=bool,
        default=True,
        doc=("When using doApplyExternalPhotoCalib, use 'global' calibrations "
             "that are not run per-tract.  When False, use per-tract photometric "
             "calibration files.")
    )
    useGlobalExternalSkyWcs = pexConfig.Field(
        dtype=bool,
        default=True,
        doc=("When using doApplyExternalSkyWcs, use 'global' calibrations "
             "that are not run per-tract.  When False, use per-tract wcs "
             "files.")
    )
    idGenerator = DetectorVisitIdGeneratorConfig.make_field()

    def validate(self):
        super().validate()
        if self.doApplyExternalSkyWcs and not self.doReevaluateSkyWcs:
            log.warning("doApplyExternalSkyWcs=True but doReevaluateSkyWcs=False"
                        "External SkyWcs will not be read or evaluated.")
        if self.doApplyExternalPhotoCalib and not self.doReevaluatePhotoCalib:
            log.warning("doApplyExternalPhotoCalib=True but doReevaluatePhotoCalib=False."
                        "External PhotoCalib will not be read or evaluated.")


class WriteRecalibratedSourceTableTask(WriteSourceTableTask):
    """Write source table to DataFrame Parquet format.
    """
    _DefaultName = "writeRecalibratedSourceTable"
    ConfigClass = WriteRecalibratedSourceTableConfig

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)

        idGenerator = self.config.idGenerator.apply(butlerQC.quantum.dataId)
        inputs['idGenerator'] = idGenerator
        inputs['ccdVisitId'] = idGenerator.catalog_id

        if self.config.doReevaluatePhotoCalib or self.config.doReevaluateSkyWcs:
            if self.config.doApplyExternalPhotoCalib or self.config.doApplyExternalSkyWcs:
                inputs['exposure'] = self.attachCalibs(inputRefs, **inputs)

            inputs['catalog'] = self.addCalibColumns(**inputs)

        result = self.run(**inputs)
        outputs = pipeBase.Struct(outputCatalog=result.table)
        butlerQC.put(outputs, outputRefs)

    def attachCalibs(self, inputRefs, skyMap, exposure, externalSkyWcsGlobalCatalog=None,
                     externalSkyWcsTractCatalog=None, externalPhotoCalibGlobalCatalog=None,
                     externalPhotoCalibTractCatalog=None, **kwargs):
        """Apply external calibrations to exposure per configuration

        When multiple tract-level calibrations overlap, select the one with the
        center closest to detector.

        Parameters
        ----------
        inputRefs : `lsst.pipe.base.InputQuantizedConnection`, for dataIds of
            tract-level calibs.
        skyMap : `lsst.skymap.SkyMap`
        exposure : `lsst.afw.image.exposure.Exposure`
            Input exposure to adjust calibrations.
        externalSkyWcsGlobalCatalog : `lsst.afw.table.ExposureCatalog`, optional
            Exposure catalog with external skyWcs to be applied per config
        externalSkyWcsTractCatalog : `lsst.afw.table.ExposureCatalog`, optional
            Exposure catalog with external skyWcs to be applied per config
        externalPhotoCalibGlobalCatalog : `lsst.afw.table.ExposureCatalog`, optional
            Exposure catalog with external photoCalib to be applied per config
        externalPhotoCalibTractCatalog : `lsst.afw.table.ExposureCatalog`, optional
            Exposure catalog with external photoCalib to be applied per config
        **kwargs
            Additional keyword arguments are ignored to facilitate passing the
            same arguments to several methods.

        Returns
        -------
        exposure : `lsst.afw.image.exposure.Exposure`
            Exposure with adjusted calibrations.
        """
        if not self.config.doApplyExternalSkyWcs:
            # Do not modify the exposure's SkyWcs
            externalSkyWcsCatalog = None
        elif self.config.useGlobalExternalSkyWcs:
            # Use the global external SkyWcs
            externalSkyWcsCatalog = externalSkyWcsGlobalCatalog
            self.log.info('Applying global SkyWcs')
        else:
            # use tract-level external SkyWcs from the closest overlapping tract
            inputRef = getattr(inputRefs, 'externalSkyWcsTractCatalog')
            tracts = [ref.dataId['tract'] for ref in inputRef]
            if len(tracts) == 1:
                ind = 0
                self.log.info('Applying tract-level SkyWcs from tract %s', tracts[ind])
            else:
                if exposure.getWcs() is None:  # TODO: could this look-up use the externalPhotoCalib?
                    raise ValueError("Trying to locate nearest tract, but exposure.wcs is None.")
                ind = self.getClosestTract(tracts, skyMap,
                                           exposure.getBBox(), exposure.getWcs())
                self.log.info('Multiple overlapping externalSkyWcsTractCatalogs found (%s). '
                              'Applying closest to detector center: tract=%s', str(tracts), tracts[ind])

            externalSkyWcsCatalog = externalSkyWcsTractCatalog[ind]

        if not self.config.doApplyExternalPhotoCalib:
            # Do not modify the exposure's PhotoCalib
            externalPhotoCalibCatalog = None
        elif self.config.useGlobalExternalPhotoCalib:
            # Use the global external PhotoCalib
            externalPhotoCalibCatalog = externalPhotoCalibGlobalCatalog
            self.log.info('Applying global PhotoCalib')
        else:
            # use tract-level external PhotoCalib from the closest overlapping tract
            inputRef = getattr(inputRefs, 'externalPhotoCalibTractCatalog')
            tracts = [ref.dataId['tract'] for ref in inputRef]
            if len(tracts) == 1:
                ind = 0
                self.log.info('Applying tract-level PhotoCalib from tract %s', tracts[ind])
            else:
                ind = self.getClosestTract(tracts, skyMap,
                                           exposure.getBBox(), exposure.getWcs())
                self.log.info('Multiple overlapping externalPhotoCalibTractCatalogs found (%s). '
                              'Applying closest to detector center: tract=%s', str(tracts), tracts[ind])

            externalPhotoCalibCatalog = externalPhotoCalibTractCatalog[ind]

        return self.prepareCalibratedExposure(exposure, externalSkyWcsCatalog, externalPhotoCalibCatalog)

    def getClosestTract(self, tracts, skyMap, bbox, wcs):
        """Find the index of the tract closest to detector from list of tractIds

        Parameters
        ----------
        tracts: `list` [`int`]
            Iterable of integer tractIds
        skyMap : `lsst.skymap.SkyMap`
            skyMap to lookup tract geometry and wcs
        bbox : `lsst.geom.Box2I`
            Detector bbox, center of which will compared to tract centers
        wcs : `lsst.afw.geom.SkyWcs`
            Detector Wcs object to map the detector center to SkyCoord

        Returns
        -------
        index : `int`
        """
        if len(tracts) == 1:
            return 0

        center = wcs.pixelToSky(bbox.getCenter())
        sep = []
        for tractId in tracts:
            tract = skyMap[tractId]
            tractCenter = tract.getWcs().pixelToSky(tract.getBBox().getCenter())
            sep.append(center.separation(tractCenter))

        return np.argmin(sep)

    def prepareCalibratedExposure(self, exposure, externalSkyWcsCatalog=None, externalPhotoCalibCatalog=None):
        """Prepare a calibrated exposure and apply external calibrations
        if so configured.

        Parameters
        ----------
        exposure : `lsst.afw.image.exposure.Exposure`
            Input exposure to adjust calibrations.
        externalSkyWcsCatalog :  `lsst.afw.table.ExposureCatalog`, optional
            Exposure catalog with external skyWcs to be applied
            if config.doApplyExternalSkyWcs=True.  Catalog uses the detector id
            for the catalog id, sorted on id for fast lookup.
        externalPhotoCalibCatalog : `lsst.afw.table.ExposureCatalog`, optional
            Exposure catalog with external photoCalib to be applied
            if config.doApplyExternalPhotoCalib=True.  Catalog uses the detector
            id for the catalog id, sorted on id for fast lookup.

        Returns
        -------
        exposure : `lsst.afw.image.exposure.Exposure`
            Exposure with adjusted calibrations.
        """
        detectorId = exposure.getInfo().getDetector().getId()

        if externalPhotoCalibCatalog is not None:
            row = externalPhotoCalibCatalog.find(detectorId)
            if row is None:
                self.log.warning("Detector id %s not found in externalPhotoCalibCatalog; "
                                 "Using original photoCalib.", detectorId)
            else:
                photoCalib = row.getPhotoCalib()
                if photoCalib is None:
                    self.log.warning("Detector id %s has None for photoCalib in externalPhotoCalibCatalog; "
                                     "Using original photoCalib.", detectorId)
                else:
                    exposure.setPhotoCalib(photoCalib)

        if externalSkyWcsCatalog is not None:
            row = externalSkyWcsCatalog.find(detectorId)
            if row is None:
                self.log.warning("Detector id %s not found in externalSkyWcsCatalog; "
                                 "Using original skyWcs.", detectorId)
            else:
                skyWcs = row.getWcs()
                if skyWcs is None:
                    self.log.warning("Detector id %s has None for skyWcs in externalSkyWcsCatalog; "
                                     "Using original skyWcs.", detectorId)
                else:
                    exposure.setWcs(skyWcs)

        return exposure

    def addCalibColumns(self, catalog, exposure, idGenerator, **kwargs):
        """Add replace columns with calibs evaluated at each centroid

        Add or replace 'base_LocalWcs' `base_LocalPhotoCalib' columns in a
        a source catalog, by rerunning the plugins.

        Parameters
        ----------
        catalog : `lsst.afw.table.SourceCatalog`
            catalog to which calib columns will be added
        exposure : `lsst.afw.image.exposure.Exposure`
            Exposure with attached PhotoCalibs and SkyWcs attributes to be
            reevaluated at local centroids. Pixels are not required.
        idGenerator : `lsst.meas.base.IdGenerator`
            Object that generates Source IDs and random seeds.
        **kwargs
            Additional keyword arguments are ignored to facilitate passing the
            same arguments to several methods.

        Returns
        -------
        newCat:  `lsst.afw.table.SourceCatalog`
            Source Catalog with requested local calib columns
        """
        measureConfig = SingleFrameMeasurementTask.ConfigClass()
        measureConfig.doReplaceWithNoise = False

        # Clear all slots, because we aren't running the relevant plugins.
        for slot in measureConfig.slots:
            setattr(measureConfig.slots, slot, None)

        measureConfig.plugins.names = []
        if self.config.doReevaluateSkyWcs:
            measureConfig.plugins.names.add('base_LocalWcs')
            self.log.info("Re-evaluating base_LocalWcs plugin")
        if self.config.doReevaluatePhotoCalib:
            measureConfig.plugins.names.add('base_LocalPhotoCalib')
            self.log.info("Re-evaluating base_LocalPhotoCalib plugin")
        pluginsNotToCopy = tuple(measureConfig.plugins.names)

        # Create a new schema and catalog
        # Copy all columns from original except for the ones to reevaluate
        aliasMap = catalog.schema.getAliasMap()
        mapper = afwTable.SchemaMapper(catalog.schema)
        for item in catalog.schema:
            if not item.field.getName().startswith(pluginsNotToCopy):
                mapper.addMapping(item.key)

        schema = mapper.getOutputSchema()
        measurement = SingleFrameMeasurementTask(config=measureConfig, schema=schema)
        schema.setAliasMap(aliasMap)
        newCat = afwTable.SourceCatalog(schema)
        newCat.extend(catalog, mapper=mapper)

        # Fluxes in sourceCatalogs are in counts, so there are no fluxes to
        # update here. LocalPhotoCalibs are applied during transform tasks.
        # Update coord_ra/coord_dec, which are expected to be positions on the
        # sky and are used as such in sdm tables without transform
        if self.config.doReevaluateSkyWcs and exposure.wcs is not None:
            afwTable.updateSourceCoords(exposure.wcs, newCat)

        measurement.run(measCat=newCat, exposure=exposure, exposureId=idGenerator.catalog_id)

        return newCat


class PostprocessAnalysis(object):
    """Calculate columns from DataFrames or handles storing DataFrames.

    This object manages and organizes an arbitrary set of computations
    on a catalog.  The catalog is defined by a
    `DeferredDatasetHandle` or `InMemoryDatasetHandle` object
    (or list thereof), such as a ``deepCoadd_obj`` dataset, and the
    computations are defined by a collection of `lsst.pipe.tasks.functor.Functor`
    objects (or, equivalently, a ``CompositeFunctor``).

    After the object is initialized, accessing the ``.df`` attribute (which
    holds the `pandas.DataFrame` containing the results of the calculations)
    triggers computation of said dataframe.

    One of the conveniences of using this object is the ability to define a
    desired common filter for all functors.  This enables the same functor
    collection to be passed to several different `PostprocessAnalysis` objects
    without having to change the original functor collection, since the ``filt``
    keyword argument of this object triggers an overwrite of the ``filt``
    property for all functors in the collection.

    This object also allows a list of refFlags to be passed, and defines a set
    of default refFlags that are always included even if not requested.

    If a list of DataFrames or Handles is passed, rather than a single one,
    then the calculations will be mapped over all the input catalogs.  In
    principle, it should be straightforward to parallelize this activity, but
    initial tests have failed (see TODO in code comments).

    Parameters
    ----------
    handles : `lsst.daf.butler.DeferredDatasetHandle` or
              `lsst.pipe.base.InMemoryDatasetHandle` or
              list of these.
        Source catalog(s) for computation.
    functors : `list`, `dict`, or `~lsst.pipe.tasks.functors.CompositeFunctor`
        Computations to do (functors that act on ``handles``).
        If a dict, the output
        DataFrame will have columns keyed accordingly.
        If a list, the column keys will come from the
        ``.shortname`` attribute of each functor.

    filt : `str`, optional
        Filter in which to calculate.  If provided,
        this will overwrite any existing ``.filt`` attribute
        of the provided functors.

    flags : `list`, optional
        List of flags (per-band) to include in output table.
        Taken from the ``meas`` dataset if applied to a multilevel Object Table.

    refFlags : `list`, optional
        List of refFlags (only reference band) to include in output table.

    forcedFlags : `list`, optional
        List of flags (per-band) to include in output table.
        Taken from the ``forced_src`` dataset if applied to a
        multilevel Object Table. Intended for flags from measurement plugins
        only run during multi-band forced-photometry.
    """
    _defaultRefFlags = []
    _defaultFuncs = ()

    def __init__(self, handles, functors, filt=None, flags=None, refFlags=None, forcedFlags=None):
        self.handles = handles
        self.functors = functors

        self.filt = filt
        self.flags = list(flags) if flags is not None else []
        self.forcedFlags = list(forcedFlags) if forcedFlags is not None else []
        self.refFlags = list(self._defaultRefFlags)
        if refFlags is not None:
            self.refFlags += list(refFlags)

        self._df = None

    @property
    def defaultFuncs(self):
        funcs = dict(self._defaultFuncs)
        return funcs

    @property
    def func(self):
        additionalFuncs = self.defaultFuncs
        additionalFuncs.update({flag: Column(flag, dataset='forced_src') for flag in self.forcedFlags})
        additionalFuncs.update({flag: Column(flag, dataset='ref') for flag in self.refFlags})
        additionalFuncs.update({flag: Column(flag, dataset='meas') for flag in self.flags})

        if isinstance(self.functors, CompositeFunctor):
            func = self.functors
        else:
            func = CompositeFunctor(self.functors)

        func.funcDict.update(additionalFuncs)
        func.filt = self.filt

        return func

    @property
    def noDupCols(self):
        return [name for name, func in self.func.funcDict.items() if func.noDup or func.dataset == 'ref']

    @property
    def df(self):
        if self._df is None:
            self.compute()
        return self._df

    def compute(self, dropna=False, pool=None):
        # map over multiple handles
        if type(self.handles) in (list, tuple):
            if pool is None:
                dflist = [self.func(handle, dropna=dropna) for handle in self.handles]
            else:
                # TODO: Figure out why this doesn't work (pyarrow pickling
                # issues?)
                dflist = pool.map(functools.partial(self.func, dropna=dropna), self.handles)
            self._df = pd.concat(dflist)
        else:
            self._df = self.func(self.handles, dropna=dropna)

        return self._df


class TransformCatalogBaseConnections(pipeBase.PipelineTaskConnections,
                                      dimensions=()):
    """Expected Connections for subclasses of TransformCatalogBaseTask.

    Must be subclassed.
    """
    inputCatalog = connectionTypes.Input(
        name="",
        storageClass="DataFrame",
    )
    outputCatalog = connectionTypes.Output(
        name="",
        storageClass="DataFrame",
    )


class TransformCatalogBaseConfig(pipeBase.PipelineTaskConfig,
                                 pipelineConnections=TransformCatalogBaseConnections):
    functorFile = pexConfig.Field(
        dtype=str,
        doc="Path to YAML file specifying Science Data Model functors to use "
            "when copying columns and computing calibrated values.",
        default=None,
        optional=True
    )
    primaryKey = pexConfig.Field(
        dtype=str,
        doc="Name of column to be set as the DataFrame index. If None, the index"
            "will be named `id`",
        default=None,
        optional=True
    )
    columnsFromDataId = pexConfig.ListField(
        dtype=str,
        default=None,
        optional=True,
        doc="Columns to extract from the dataId",
    )


class TransformCatalogBaseTask(pipeBase.PipelineTask):
    """Base class for transforming/standardizing a catalog

    by applying functors that convert units and apply calibrations.
    The purpose of this task is to perform a set of computations on
    an input ``DeferredDatasetHandle`` or ``InMemoryDatasetHandle`` that holds
    a ``DataFrame`` dataset (such as ``deepCoadd_obj``), and write the
    results to a new dataset (which needs to be declared in an ``outputDataset``
    attribute).

    The calculations to be performed are defined in a YAML file that specifies
    a set of functors to be computed, provided as
    a ``--functorFile`` config parameter.  An example of such a YAML file
    is the following:

        funcs:
            psfMag:
                functor: Mag
                args:
                    - base_PsfFlux
                filt: HSC-G
                dataset: meas
            cmodel_magDiff:
                functor: MagDiff
                args:
                    - modelfit_CModel
                    - base_PsfFlux
                filt: HSC-G
            gauss_magDiff:
                functor: MagDiff
                args:
                    - base_GaussianFlux
                    - base_PsfFlux
                filt: HSC-G
            count:
                functor: Column
                args:
                    - base_InputCount_value
                filt: HSC-G
            deconvolved_moments:
                functor: DeconvolvedMoments
                filt: HSC-G
                dataset: forced_src
        refFlags:
            - calib_psfUsed
            - merge_measurement_i
            - merge_measurement_r
            - merge_measurement_z
            - merge_measurement_y
            - merge_measurement_g
            - base_PixelFlags_flag_inexact_psfCenter
            - detect_isPrimary

    The names for each entry under "func" will become the names of columns in
    the output dataset.  All the functors referenced are defined in
    `lsst.pipe.tasks.functors`.  Positional arguments to be passed to each
    functor are in the `args` list, and any additional entries for each column
    other than "functor" or "args" (e.g., ``'filt'``, ``'dataset'``) are treated as
    keyword arguments to be passed to the functor initialization.

    The "flags" entry is the default shortcut for `Column` functors.
    All columns listed under "flags" will be copied to the output table
    untransformed. They can be of any datatype.
    In the special case of transforming a multi-level oject table with
    band and dataset indices (deepCoadd_obj), these will be taked from the
    `meas` dataset and exploded out per band.

    There are two special shortcuts that only apply when transforming
    multi-level Object (deepCoadd_obj) tables:
     -  The "refFlags" entry is shortcut for `Column` functor
        taken from the `'ref'` dataset if transforming an ObjectTable.
     -  The "forcedFlags" entry is shortcut for `Column` functors.
        taken from the ``forced_src`` dataset if transforming an ObjectTable.
        These are expanded out per band.


    This task uses the `lsst.pipe.tasks.postprocess.PostprocessAnalysis` object
    to organize and excecute the calculations.
    """
    @property
    def _DefaultName(self):
        raise NotImplementedError('Subclass must define "_DefaultName" attribute')

    @property
    def outputDataset(self):
        raise NotImplementedError('Subclass must define "outputDataset" attribute')

    @property
    def inputDataset(self):
        raise NotImplementedError('Subclass must define "inputDataset" attribute')

    @property
    def ConfigClass(self):
        raise NotImplementedError('Subclass must define "ConfigClass" attribute')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.config.functorFile:
            self.log.info('Loading tranform functor definitions from %s',
                          self.config.functorFile)
            self.funcs = CompositeFunctor.from_file(self.config.functorFile)
            self.funcs.update(dict(PostprocessAnalysis._defaultFuncs))
        else:
            self.funcs = None

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)
        if self.funcs is None:
            raise ValueError("config.functorFile is None. "
                             "Must be a valid path to yaml in order to run Task as a PipelineTask.")
        result = self.run(handle=inputs['inputCatalog'], funcs=self.funcs,
                          dataId=outputRefs.outputCatalog.dataId.full)
        outputs = pipeBase.Struct(outputCatalog=result)
        butlerQC.put(outputs, outputRefs)

    def run(self, handle, funcs=None, dataId=None, band=None):
        """Do postprocessing calculations

        Takes a ``DeferredDatasetHandle`` or ``InMemoryDatasetHandle`` or
        ``DataFrame`` object and dataId,
        returns a dataframe with results of postprocessing calculations.

        Parameters
        ----------
        handles : `lsst.daf.butler.DeferredDatasetHandle` or
                  `lsst.pipe.base.InMemoryDatasetHandle` or
                  `pandas.DataFrame`, or list of these.
            DataFrames from which calculations are done.
        funcs : `lsst.pipe.tasks.functors.Functors`
            Functors to apply to the table's columns
        dataId : dict, optional
            Used to add a `patchId` column to the output dataframe.
        band : `str`, optional
            Filter band that is being processed.

        Returns
        ------
        df : `pandas.DataFrame`
        """
        self.log.info("Transforming/standardizing the source table dataId: %s", dataId)

        df = self.transform(band, handle, funcs, dataId).df
        self.log.info("Made a table of %d columns and %d rows", len(df.columns), len(df))
        return df

    def getFunctors(self):
        return self.funcs

    def getAnalysis(self, handles, funcs=None, band=None):
        if funcs is None:
            funcs = self.funcs
        analysis = PostprocessAnalysis(handles, funcs, filt=band)
        return analysis

    def transform(self, band, handles, funcs, dataId):
        analysis = self.getAnalysis(handles, funcs=funcs, band=band)
        df = analysis.df
        if dataId and self.config.columnsFromDataId:
            for key in self.config.columnsFromDataId:
                if key in dataId:
                    df[str(key)] = dataId[key]
                else:
                    raise ValueError(f"'{key}' in config.columnsFromDataId not found in dataId: {dataId}")

        if self.config.primaryKey:
            if df.index.name != self.config.primaryKey and self.config.primaryKey in df:
                df.reset_index(inplace=True, drop=True)
                df.set_index(self.config.primaryKey, inplace=True)

        return pipeBase.Struct(
            df=df,
            analysis=analysis
        )


class TransformObjectCatalogConnections(pipeBase.PipelineTaskConnections,
                                        defaultTemplates={"coaddName": "deep"},
                                        dimensions=("tract", "patch", "skymap")):
    inputCatalog = connectionTypes.Input(
        doc="The vertical concatenation of the deepCoadd_{ref|meas|forced_src} catalogs, "
            "stored as a DataFrame with a multi-level column index per-patch.",
        dimensions=("tract", "patch", "skymap"),
        storageClass="DataFrame",
        name="{coaddName}Coadd_obj",
        deferLoad=True,
    )
    outputCatalog = connectionTypes.Output(
        doc="Per-Patch Object Table of columns transformed from the deepCoadd_obj table per the standard "
            "data model.",
        dimensions=("tract", "patch", "skymap"),
        storageClass="DataFrame",
        name="objectTable"
    )


class TransformObjectCatalogConfig(TransformCatalogBaseConfig,
                                   pipelineConnections=TransformObjectCatalogConnections):
    coaddName = pexConfig.Field(
        dtype=str,
        default="deep",
        doc="Name of coadd"
    )
    # TODO: remove in DM-27177
    filterMap = pexConfig.DictField(
        keytype=str,
        itemtype=str,
        default={},
        doc=("Dictionary mapping full filter name to short one for column name munging."
             "These filters determine the output columns no matter what filters the "
             "input data actually contain."),
        deprecated=("Coadds are now identified by the band, so this transform is unused."
                    "Will be removed after v22.")
    )
    outputBands = pexConfig.ListField(
        dtype=str,
        default=None,
        optional=True,
        doc=("These bands and only these bands will appear in the output,"
             " NaN-filled if the input does not include them."
             " If None, then use all bands found in the input.")
    )
    camelCase = pexConfig.Field(
        dtype=bool,
        default=False,
        doc=("Write per-band columns names with camelCase, else underscore "
             "For example: gPsFlux instead of g_PsFlux.")
    )
    multilevelOutput = pexConfig.Field(
        dtype=bool,
        default=False,
        doc=("Whether results dataframe should have a multilevel column index (True) or be flat "
             "and name-munged (False).")
    )
    goodFlags = pexConfig.ListField(
        dtype=str,
        default=[],
        doc=("List of 'good' flags that should be set False when populating empty tables. "
             "All other flags are considered to be 'bad' flags and will be set to True.")
    )
    floatFillValue = pexConfig.Field(
        dtype=float,
        default=np.nan,
        doc="Fill value for float fields when populating empty tables."
    )
    integerFillValue = pexConfig.Field(
        dtype=int,
        default=-1,
        doc="Fill value for integer fields when populating empty tables."
    )

    def setDefaults(self):
        super().setDefaults()
        self.functorFile = os.path.join('$PIPE_TASKS_DIR', 'schemas', 'Object.yaml')
        self.primaryKey = 'objectId'
        self.columnsFromDataId = ['tract', 'patch']
        self.goodFlags = ['calib_astrometry_used',
                          'calib_photometry_reserved',
                          'calib_photometry_used',
                          'calib_psf_candidate',
                          'calib_psf_reserved',
                          'calib_psf_used']


class TransformObjectCatalogTask(TransformCatalogBaseTask):
    """Produce a flattened Object Table to match the format specified in
    sdm_schemas.

    Do the same set of postprocessing calculations on all bands.

    This is identical to `TransformCatalogBaseTask`, except for that it does
    the specified functor calculations for all filters present in the
    input `deepCoadd_obj` table.  Any specific ``"filt"`` keywords specified
    by the YAML file will be superceded.
    """
    _DefaultName = "transformObjectCatalog"
    ConfigClass = TransformObjectCatalogConfig

    def run(self, handle, funcs=None, dataId=None, band=None):
        # NOTE: band kwarg is ignored here.
        dfDict = {}
        analysisDict = {}
        templateDf = pd.DataFrame()

        columns = handle.get(component='columns')
        inputBands = columns.unique(level=1).values

        outputBands = self.config.outputBands if self.config.outputBands else inputBands

        # Perform transform for data of filters that exist in the handle dataframe.
        for inputBand in inputBands:
            if inputBand not in outputBands:
                self.log.info("Ignoring %s band data in the input", inputBand)
                continue
            self.log.info("Transforming the catalog of band %s", inputBand)
            result = self.transform(inputBand, handle, funcs, dataId)
            dfDict[inputBand] = result.df
            analysisDict[inputBand] = result.analysis
            if templateDf.empty:
                templateDf = result.df

        # Put filler values in columns of other wanted bands
        for filt in outputBands:
            if filt not in dfDict:
                self.log.info("Adding empty columns for band %s", filt)
                dfTemp = templateDf.copy()
                for col in dfTemp.columns:
                    testValue = dfTemp[col].values[0]
                    if isinstance(testValue, (np.bool_, pd.BooleanDtype)):
                        # Boolean flag type, check if it is a "good" flag
                        if col in self.config.goodFlags:
                            fillValue = False
                        else:
                            fillValue = True
                    elif isinstance(testValue, numbers.Integral):
                        # Checking numbers.Integral catches all flavors
                        # of python, numpy, pandas, etc. integers.
                        # We must ensure this is not an unsigned integer.
                        if isinstance(testValue, np.unsignedinteger):
                            raise ValueError("Parquet tables may not have unsigned integer columns.")
                        else:
                            fillValue = self.config.integerFillValue
                    else:
                        fillValue = self.config.floatFillValue
                    dfTemp[col].values[:] = fillValue
                dfDict[filt] = dfTemp

        # This makes a multilevel column index, with band as first level
        df = pd.concat(dfDict, axis=1, names=['band', 'column'])

        if not self.config.multilevelOutput:
            noDupCols = list(set.union(*[set(v.noDupCols) for v in analysisDict.values()]))
            if self.config.primaryKey in noDupCols:
                noDupCols.remove(self.config.primaryKey)
            if dataId and self.config.columnsFromDataId:
                noDupCols += self.config.columnsFromDataId
            df = flattenFilters(df, noDupCols=noDupCols, camelCase=self.config.camelCase,
                                inputBands=inputBands)

        self.log.info("Made a table of %d columns and %d rows", len(df.columns), len(df))

        return df


class ConsolidateObjectTableConnections(pipeBase.PipelineTaskConnections,
                                        dimensions=("tract", "skymap")):
    inputCatalogs = connectionTypes.Input(
        doc="Per-Patch objectTables conforming to the standard data model.",
        name="objectTable",
        storageClass="DataFrame",
        dimensions=("tract", "patch", "skymap"),
        multiple=True,
    )
    outputCatalog = connectionTypes.Output(
        doc="Pre-tract horizontal concatenation of the input objectTables",
        name="objectTable_tract",
        storageClass="DataFrame",
        dimensions=("tract", "skymap"),
    )


class ConsolidateObjectTableConfig(pipeBase.PipelineTaskConfig,
                                   pipelineConnections=ConsolidateObjectTableConnections):
    coaddName = pexConfig.Field(
        dtype=str,
        default="deep",
        doc="Name of coadd"
    )


class ConsolidateObjectTableTask(pipeBase.PipelineTask):
    """Write patch-merged source tables to a tract-level DataFrame Parquet file.

    Concatenates `objectTable` list into a per-visit `objectTable_tract`.
    """
    _DefaultName = "consolidateObjectTable"
    ConfigClass = ConsolidateObjectTableConfig

    inputDataset = 'objectTable'
    outputDataset = 'objectTable_tract'

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)
        self.log.info("Concatenating %s per-patch Object Tables",
                      len(inputs['inputCatalogs']))
        df = pd.concat(inputs['inputCatalogs'])
        butlerQC.put(pipeBase.Struct(outputCatalog=df), outputRefs)


class TransformSourceTableConnections(pipeBase.PipelineTaskConnections,
                                      defaultTemplates={"catalogType": ""},
                                      dimensions=("instrument", "visit", "detector")):

    inputCatalog = connectionTypes.Input(
        doc="Wide input catalog of sources produced by WriteSourceTableTask",
        name="{catalogType}source",
        storageClass="DataFrame",
        dimensions=("instrument", "visit", "detector"),
        deferLoad=True
    )
    outputCatalog = connectionTypes.Output(
        doc="Narrower, per-detector Source Table transformed and converted per a "
            "specified set of functors",
        name="{catalogType}sourceTable",
        storageClass="DataFrame",
        dimensions=("instrument", "visit", "detector")
    )


class TransformSourceTableConfig(TransformCatalogBaseConfig,
                                 pipelineConnections=TransformSourceTableConnections):

    def setDefaults(self):
        super().setDefaults()
        self.functorFile = os.path.join('$PIPE_TASKS_DIR', 'schemas', 'Source.yaml')
        self.primaryKey = 'sourceId'
        self.columnsFromDataId = ['visit', 'detector', 'band', 'physical_filter']


class TransformSourceTableTask(TransformCatalogBaseTask):
    """Transform/standardize a source catalog
    """
    _DefaultName = "transformSourceTable"
    ConfigClass = TransformSourceTableConfig


class ConsolidateVisitSummaryConnections(pipeBase.PipelineTaskConnections,
                                         dimensions=("instrument", "visit",),
                                         defaultTemplates={"calexpType": ""}):
    calexp = connectionTypes.Input(
        doc="Processed exposures used for metadata",
        name="calexp",
        storageClass="ExposureF",
        dimensions=("instrument", "visit", "detector"),
        deferLoad=True,
        multiple=True,
    )
    visitSummary = connectionTypes.Output(
        doc=("Per-visit consolidated exposure metadata.  These catalogs use "
             "detector id for the id and are sorted for fast lookups of a "
             "detector."),
        name="visitSummary",
        storageClass="ExposureCatalog",
        dimensions=("instrument", "visit"),
    )
    visitSummarySchema = connectionTypes.InitOutput(
        doc="Schema of the visitSummary catalog",
        name="visitSummary_schema",
        storageClass="ExposureCatalog",
    )


class ConsolidateVisitSummaryConfig(pipeBase.PipelineTaskConfig,
                                    pipelineConnections=ConsolidateVisitSummaryConnections):
    """Config for ConsolidateVisitSummaryTask"""
    pass


class ConsolidateVisitSummaryTask(pipeBase.PipelineTask):
    """Task to consolidate per-detector visit metadata.

    This task aggregates the following metadata from all the detectors in a
    single visit into an exposure catalog:
    - The visitInfo.
    - The wcs.
    - The photoCalib.
    - The physical_filter and band (if available).
    - The psf size, shape, and effective area at the center of the detector.
    - The corners of the bounding box in right ascension/declination.

    Other quantities such as Detector, Psf, ApCorrMap, and TransmissionCurve
    are not persisted here because of storage concerns, and because of their
    limited utility as summary statistics.

    Tests for this task are performed in ci_hsc_gen3.
    """
    _DefaultName = "consolidateVisitSummary"
    ConfigClass = ConsolidateVisitSummaryConfig

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.schema = afwTable.ExposureTable.makeMinimalSchema()
        self.schema.addField('visit', type='L', doc='Visit number')
        self.schema.addField('physical_filter', type='String', size=32, doc='Physical filter')
        self.schema.addField('band', type='String', size=32, doc='Name of band')
        ExposureSummaryStats.update_schema(self.schema)
        self.visitSummarySchema = afwTable.ExposureCatalog(self.schema)

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        dataRefs = butlerQC.get(inputRefs.calexp)
        visit = dataRefs[0].dataId.byName()['visit']

        self.log.debug("Concatenating metadata from %d per-detector calexps (visit %d)",
                       len(dataRefs), visit)

        expCatalog = self._combineExposureMetadata(visit, dataRefs)

        butlerQC.put(expCatalog, outputRefs.visitSummary)

    def _combineExposureMetadata(self, visit, dataRefs):
        """Make a combined exposure catalog from a list of dataRefs.
        These dataRefs must point to exposures with wcs, summaryStats,
        and other visit metadata.

        Parameters
        ----------
        visit : `int`
            Visit identification number.
        dataRefs : `list` of `lsst.daf.butler.DeferredDatasetHandle`
            List of dataRefs in visit.

        Returns
        -------
        visitSummary : `lsst.afw.table.ExposureCatalog`
            Exposure catalog with per-detector summary information.
        """
        cat = afwTable.ExposureCatalog(self.schema)
        cat.resize(len(dataRefs))

        cat['visit'] = visit

        for i, dataRef in enumerate(dataRefs):
            visitInfo = dataRef.get(component='visitInfo')
            filterLabel = dataRef.get(component='filter')
            summaryStats = dataRef.get(component='summaryStats')
            detector = dataRef.get(component='detector')
            wcs = dataRef.get(component='wcs')
            photoCalib = dataRef.get(component='photoCalib')
            detector = dataRef.get(component='detector')
            bbox = dataRef.get(component='bbox')
            validPolygon = dataRef.get(component='validPolygon')

            rec = cat[i]
            rec.setBBox(bbox)
            rec.setVisitInfo(visitInfo)
            rec.setWcs(wcs)
            rec.setPhotoCalib(photoCalib)
            rec.setValidPolygon(validPolygon)

            rec['physical_filter'] = filterLabel.physicalLabel if filterLabel.hasPhysicalLabel() else ""
            rec['band'] = filterLabel.bandLabel if filterLabel.hasBandLabel() else ""
            rec.setId(detector.getId())
            summaryStats.update_record(rec)

        metadata = dafBase.PropertyList()
        metadata.add("COMMENT", "Catalog id is detector id, sorted.")
        # We are looping over existing datarefs, so the following is true
        metadata.add("COMMENT", "Only detectors with data have entries.")
        cat.setMetadata(metadata)

        cat.sort()
        return cat


class ConsolidateSourceTableConnections(pipeBase.PipelineTaskConnections,
                                        defaultTemplates={"catalogType": ""},
                                        dimensions=("instrument", "visit")):
    inputCatalogs = connectionTypes.Input(
        doc="Input per-detector Source Tables",
        name="{catalogType}sourceTable",
        storageClass="DataFrame",
        dimensions=("instrument", "visit", "detector"),
        multiple=True
    )
    outputCatalog = connectionTypes.Output(
        doc="Per-visit concatenation of Source Table",
        name="{catalogType}sourceTable_visit",
        storageClass="DataFrame",
        dimensions=("instrument", "visit")
    )


class ConsolidateSourceTableConfig(pipeBase.PipelineTaskConfig,
                                   pipelineConnections=ConsolidateSourceTableConnections):
    pass


class ConsolidateSourceTableTask(pipeBase.PipelineTask):
    """Concatenate `sourceTable` list into a per-visit `sourceTable_visit`
    """
    _DefaultName = 'consolidateSourceTable'
    ConfigClass = ConsolidateSourceTableConfig

    inputDataset = 'sourceTable'
    outputDataset = 'sourceTable_visit'

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        from .makeWarp import reorderRefs

        detectorOrder = [ref.dataId['detector'] for ref in inputRefs.inputCatalogs]
        detectorOrder.sort()
        inputRefs = reorderRefs(inputRefs, detectorOrder, dataIdKey='detector')
        inputs = butlerQC.get(inputRefs)
        self.log.info("Concatenating %s per-detector Source Tables",
                      len(inputs['inputCatalogs']))
        df = pd.concat(inputs['inputCatalogs'])
        butlerQC.put(pipeBase.Struct(outputCatalog=df), outputRefs)


class MakeCcdVisitTableConnections(pipeBase.PipelineTaskConnections,
                                   dimensions=("instrument",),
                                   defaultTemplates={"calexpType": ""}):
    visitSummaryRefs = connectionTypes.Input(
        doc="Data references for per-visit consolidated exposure metadata",
        name="finalVisitSummary",
        storageClass="ExposureCatalog",
        dimensions=("instrument", "visit"),
        multiple=True,
        deferLoad=True,
    )
    outputCatalog = connectionTypes.Output(
        doc="CCD and Visit metadata table",
        name="ccdVisitTable",
        storageClass="DataFrame",
        dimensions=("instrument",)
    )


class MakeCcdVisitTableConfig(pipeBase.PipelineTaskConfig,
                              pipelineConnections=MakeCcdVisitTableConnections):
    idGenerator = DetectorVisitIdGeneratorConfig.make_field()


class MakeCcdVisitTableTask(pipeBase.PipelineTask):
    """Produce a `ccdVisitTable` from the visit summary exposure catalogs.
    """
    _DefaultName = 'makeCcdVisitTable'
    ConfigClass = MakeCcdVisitTableConfig

    def run(self, visitSummaryRefs):
        """Make a table of ccd information from the visit summary catalogs.

        Parameters
        ----------
        visitSummaryRefs : `list` of `lsst.daf.butler.DeferredDatasetHandle`
            List of DeferredDatasetHandles pointing to exposure catalogs with
            per-detector summary information.

        Returns
        -------
        result : `lsst.pipe.Base.Struct`
           Results struct with attribute:

           ``outputCatalog``
               Catalog of ccd and visit information.
        """
        ccdEntries = []
        for visitSummaryRef in visitSummaryRefs:
            visitSummary = visitSummaryRef.get()
            visitInfo = visitSummary[0].getVisitInfo()

            ccdEntry = {}
            summaryTable = visitSummary.asAstropy()
            selectColumns = ['id', 'visit', 'physical_filter', 'band', 'ra', 'decl', 'zenithDistance',
                             'zeroPoint', 'psfSigma', 'skyBg', 'skyNoise',
                             'astromOffsetMean', 'astromOffsetStd', 'nPsfStar',
                             'psfStarDeltaE1Median', 'psfStarDeltaE2Median',
                             'psfStarDeltaE1Scatter', 'psfStarDeltaE2Scatter',
                             'psfStarDeltaSizeMedian', 'psfStarDeltaSizeScatter',
                             'psfStarScaledDeltaSizeScatter',
                             'psfTraceRadiusDelta', 'maxDistToNearestPsf']
            ccdEntry = summaryTable[selectColumns].to_pandas().set_index('id')
            # 'visit' is the human readable visit number.
            # 'visitId' is the key to the visitId table. They are the same.
            # Technically you should join to get the visit from the visit
            # table.
            ccdEntry = ccdEntry.rename(columns={"visit": "visitId"})
            ccdEntry['ccdVisitId'] = [
                self.config.idGenerator.apply(
                    visitSummaryRef.dataId,
                    detector=detector_id,
                    is_exposure=False,
                ).catalog_id  # The "catalog ID" here is the ccdVisit ID
                              # because it's usually the ID for a whole catalog
                              # with a {visit, detector}, and that's the main
                              # use case for IdGenerator.  This usage for a
                              # summary table is rare.
                for detector_id in summaryTable['id']
            ]
            ccdEntry['detector'] = summaryTable['id']
            pixToArcseconds = np.array([vR.getWcs().getPixelScale().asArcseconds() if vR.getWcs()
                                        else np.nan for vR in visitSummary])
            ccdEntry["seeing"] = visitSummary['psfSigma'] * np.sqrt(8 * np.log(2)) * pixToArcseconds

            ccdEntry["skyRotation"] = visitInfo.getBoresightRotAngle().asDegrees()
            ccdEntry["expMidpt"] = visitInfo.getDate().toPython()
            ccdEntry["expMidptMJD"] = visitInfo.getDate().get(dafBase.DateTime.MJD)
            expTime = visitInfo.getExposureTime()
            ccdEntry['expTime'] = expTime
            ccdEntry["obsStart"] = ccdEntry["expMidpt"] - 0.5 * pd.Timedelta(seconds=expTime)
            expTime_days = expTime / (60*60*24)
            ccdEntry["obsStartMJD"] = ccdEntry["expMidptMJD"] - 0.5 * expTime_days
            ccdEntry['darkTime'] = visitInfo.getDarkTime()
            ccdEntry['xSize'] = summaryTable['bbox_max_x'] - summaryTable['bbox_min_x']
            ccdEntry['ySize'] = summaryTable['bbox_max_y'] - summaryTable['bbox_min_y']
            ccdEntry['llcra'] = summaryTable['raCorners'][:, 0]
            ccdEntry['llcdec'] = summaryTable['decCorners'][:, 0]
            ccdEntry['ulcra'] = summaryTable['raCorners'][:, 1]
            ccdEntry['ulcdec'] = summaryTable['decCorners'][:, 1]
            ccdEntry['urcra'] = summaryTable['raCorners'][:, 2]
            ccdEntry['urcdec'] = summaryTable['decCorners'][:, 2]
            ccdEntry['lrcra'] = summaryTable['raCorners'][:, 3]
            ccdEntry['lrcdec'] = summaryTable['decCorners'][:, 3]
            # TODO: DM-30618, Add raftName, nExposures, ccdTemp, binX, binY,
            # and flags, and decide if WCS, and llcx, llcy, ulcx, ulcy, etc.
            # values are actually wanted.
            ccdEntries.append(ccdEntry)

        outputCatalog = pd.concat(ccdEntries)
        outputCatalog.set_index('ccdVisitId', inplace=True, verify_integrity=True)
        return pipeBase.Struct(outputCatalog=outputCatalog)


class MakeVisitTableConnections(pipeBase.PipelineTaskConnections,
                                dimensions=("instrument",),
                                defaultTemplates={"calexpType": ""}):
    visitSummaries = connectionTypes.Input(
        doc="Per-visit consolidated exposure metadata",
        name="finalVisitSummary",
        storageClass="ExposureCatalog",
        dimensions=("instrument", "visit",),
        multiple=True,
        deferLoad=True,
    )
    outputCatalog = connectionTypes.Output(
        doc="Visit metadata table",
        name="visitTable",
        storageClass="DataFrame",
        dimensions=("instrument",)
    )


class MakeVisitTableConfig(pipeBase.PipelineTaskConfig,
                           pipelineConnections=MakeVisitTableConnections):
    pass


class MakeVisitTableTask(pipeBase.PipelineTask):
    """Produce a `visitTable` from the visit summary exposure catalogs.
    """
    _DefaultName = 'makeVisitTable'
    ConfigClass = MakeVisitTableConfig

    def run(self, visitSummaries):
        """Make a table of visit information from the visit summary catalogs.

        Parameters
        ----------
        visitSummaries : `list` of `lsst.afw.table.ExposureCatalog`
            List of exposure catalogs with per-detector summary information.
        Returns
        -------
        result : `lsst.pipe.Base.Struct`
            Results struct with attribute:

            ``outputCatalog``
                 Catalog of visit information.
        """
        visitEntries = []
        for visitSummary in visitSummaries:
            visitSummary = visitSummary.get()
            visitRow = visitSummary[0]
            visitInfo = visitRow.getVisitInfo()

            visitEntry = {}
            visitEntry["visitId"] = visitRow['visit']
            visitEntry["visit"] = visitRow['visit']
            visitEntry["physical_filter"] = visitRow['physical_filter']
            visitEntry["band"] = visitRow['band']
            raDec = visitInfo.getBoresightRaDec()
            visitEntry["ra"] = raDec.getRa().asDegrees()
            visitEntry["decl"] = raDec.getDec().asDegrees()
            visitEntry["skyRotation"] = visitInfo.getBoresightRotAngle().asDegrees()
            azAlt = visitInfo.getBoresightAzAlt()
            visitEntry["azimuth"] = azAlt.getLongitude().asDegrees()
            visitEntry["altitude"] = azAlt.getLatitude().asDegrees()
            visitEntry["zenithDistance"] = 90 - azAlt.getLatitude().asDegrees()
            visitEntry["airmass"] = visitInfo.getBoresightAirmass()
            expTime = visitInfo.getExposureTime()
            visitEntry["expTime"] = expTime
            visitEntry["expMidpt"] = visitInfo.getDate().toPython()
            visitEntry["expMidptMJD"] = visitInfo.getDate().get(dafBase.DateTime.MJD)
            visitEntry["obsStart"] = visitEntry["expMidpt"] - 0.5 * pd.Timedelta(seconds=expTime)
            expTime_days = expTime / (60*60*24)
            visitEntry["obsStartMJD"] = visitEntry["expMidptMJD"] - 0.5 * expTime_days
            visitEntries.append(visitEntry)

            # TODO: DM-30623, Add programId, exposureType, cameraTemp,
            # mirror1Temp, mirror2Temp, mirror3Temp, domeTemp, externalTemp,
            # dimmSeeing, pwvGPS, pwvMW, flags, nExposures.

        outputCatalog = pd.DataFrame(data=visitEntries)
        outputCatalog.set_index('visitId', inplace=True, verify_integrity=True)
        return pipeBase.Struct(outputCatalog=outputCatalog)


class WriteForcedSourceTableConnections(pipeBase.PipelineTaskConnections,
                                        dimensions=("instrument", "visit", "detector", "skymap", "tract")):

    inputCatalog = connectionTypes.Input(
        doc="Primary per-detector, single-epoch forced-photometry catalog. "
            "By default, it is the output of ForcedPhotCcdTask on calexps",
        name="forced_src",
        storageClass="SourceCatalog",
        dimensions=("instrument", "visit", "detector", "skymap", "tract")
    )
    inputCatalogDiff = connectionTypes.Input(
        doc="Secondary multi-epoch, per-detector, forced photometry catalog. "
            "By default, it is the output of ForcedPhotCcdTask run on image differences.",
        name="forced_diff",
        storageClass="SourceCatalog",
        dimensions=("instrument", "visit", "detector", "skymap", "tract")
    )
    outputCatalog = connectionTypes.Output(
        doc="InputCatalogs horizonatally joined on `objectId` in DataFrame parquet format",
        name="mergedForcedSource",
        storageClass="DataFrame",
        dimensions=("instrument", "visit", "detector", "skymap", "tract")
    )


class WriteForcedSourceTableConfig(pipeBase.PipelineTaskConfig,
                                   pipelineConnections=WriteForcedSourceTableConnections):
    key = lsst.pex.config.Field(
        doc="Column on which to join the two input tables on and make the primary key of the output",
        dtype=str,
        default="objectId",
    )
    idGenerator = DetectorVisitIdGeneratorConfig.make_field()


class WriteForcedSourceTableTask(pipeBase.PipelineTask):
    """Merge and convert per-detector forced source catalogs to DataFrame Parquet format.

    Because the predecessor ForcedPhotCcdTask operates per-detector,
    per-tract, (i.e., it has tract in its dimensions), detectors
    on the tract boundary may have multiple forced source catalogs.

    The successor task TransformForcedSourceTable runs per-patch
    and temporally-aggregates overlapping mergedForcedSource catalogs from all
    available multiple epochs.
    """
    _DefaultName = "writeForcedSourceTable"
    ConfigClass = WriteForcedSourceTableConfig

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)
        # Add ccdVisitId to allow joining with CcdVisitTable
        idGenerator = self.config.idGenerator.apply(butlerQC.quantum.dataId)
        inputs['ccdVisitId'] = idGenerator.catalog_id
        inputs['band'] = butlerQC.quantum.dataId.full['band']
        outputs = self.run(**inputs)
        butlerQC.put(outputs, outputRefs)

    def run(self, inputCatalog, inputCatalogDiff, ccdVisitId=None, band=None):
        dfs = []
        for table, dataset, in zip((inputCatalog, inputCatalogDiff), ('calexp', 'diff')):
            df = table.asAstropy().to_pandas().set_index(self.config.key, drop=False)
            df = df.reindex(sorted(df.columns), axis=1)
            df['ccdVisitId'] = ccdVisitId if ccdVisitId else pd.NA
            df['band'] = band if band else pd.NA
            df.columns = pd.MultiIndex.from_tuples([(dataset, c) for c in df.columns],
                                                   names=('dataset', 'column'))

            dfs.append(df)

        outputCatalog = functools.reduce(lambda d1, d2: d1.join(d2), dfs)
        return pipeBase.Struct(outputCatalog=outputCatalog)


class TransformForcedSourceTableConnections(pipeBase.PipelineTaskConnections,
                                            dimensions=("instrument", "skymap", "patch", "tract")):

    inputCatalogs = connectionTypes.Input(
        doc="DataFrames of merged ForcedSources produced by WriteForcedSourceTableTask",
        name="mergedForcedSource",
        storageClass="DataFrame",
        dimensions=("instrument", "visit", "detector", "skymap", "tract"),
        multiple=True,
        deferLoad=True
    )
    referenceCatalog = connectionTypes.Input(
        doc="Reference catalog which was used to seed the forcedPhot. Columns "
            "objectId, detect_isPrimary, detect_isTractInner, detect_isPatchInner "
            "are expected.",
        name="objectTable",
        storageClass="DataFrame",
        dimensions=("tract", "patch", "skymap"),
        deferLoad=True
    )
    outputCatalog = connectionTypes.Output(
        doc="Narrower, temporally-aggregated, per-patch ForcedSource Table transformed and converted per a "
            "specified set of functors",
        name="forcedSourceTable",
        storageClass="DataFrame",
        dimensions=("tract", "patch", "skymap")
    )


class TransformForcedSourceTableConfig(TransformCatalogBaseConfig,
                                       pipelineConnections=TransformForcedSourceTableConnections):
    referenceColumns = pexConfig.ListField(
        dtype=str,
        default=["detect_isPrimary", "detect_isTractInner", "detect_isPatchInner"],
        optional=True,
        doc="Columns to pull from reference catalog",
    )
    keyRef = lsst.pex.config.Field(
        doc="Column on which to join the two input tables on and make the primary key of the output",
        dtype=str,
        default="objectId",
    )
    key = lsst.pex.config.Field(
        doc="Rename the output DataFrame index to this name",
        dtype=str,
        default="forcedSourceId",
    )

    def setDefaults(self):
        super().setDefaults()
        self.functorFile = os.path.join('$PIPE_TASKS_DIR', 'schemas', 'ForcedSource.yaml')
        self.columnsFromDataId = ['tract', 'patch']


class TransformForcedSourceTableTask(TransformCatalogBaseTask):
    """Transform/standardize a ForcedSource catalog

    Transforms each wide, per-detector forcedSource DataFrame per the
    specification file (per-camera defaults found in ForcedSource.yaml).
    All epochs that overlap the patch are aggregated into one per-patch
    narrow-DataFrame file.

    No de-duplication of rows is performed. Duplicate resolutions flags are
    pulled in from the referenceCatalog: `detect_isPrimary`,
    `detect_isTractInner`,`detect_isPatchInner`, so that user may de-duplicate
    for analysis or compare duplicates for QA.

    The resulting table includes multiple bands. Epochs (MJDs) and other useful
    per-visit rows can be retreived by joining with the CcdVisitTable on
    ccdVisitId.
    """
    _DefaultName = "transformForcedSourceTable"
    ConfigClass = TransformForcedSourceTableConfig

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)
        if self.funcs is None:
            raise ValueError("config.functorFile is None. "
                             "Must be a valid path to yaml in order to run Task as a PipelineTask.")
        outputs = self.run(inputs['inputCatalogs'], inputs['referenceCatalog'], funcs=self.funcs,
                           dataId=outputRefs.outputCatalog.dataId.full)

        butlerQC.put(outputs, outputRefs)

    def run(self, inputCatalogs, referenceCatalog, funcs=None, dataId=None, band=None):
        dfs = []
        ref = referenceCatalog.get(parameters={"columns": self.config.referenceColumns})
        self.log.info("Aggregating %s input catalogs" % (len(inputCatalogs)))
        for handle in inputCatalogs:
            result = self.transform(None, handle, funcs, dataId)
            # Filter for only rows that were detected on (overlap) the patch
            dfs.append(result.df.join(ref, how='inner'))

        outputCatalog = pd.concat(dfs)

        # Now that we are done joining on config.keyRef
        # Change index to config.key by
        outputCatalog.index.rename(self.config.keyRef, inplace=True)
        # Add config.keyRef to the column list
        outputCatalog.reset_index(inplace=True)
        # Set the forcedSourceId to the index. This is specified in the
        # ForcedSource.yaml
        outputCatalog.set_index("forcedSourceId", inplace=True, verify_integrity=True)
        # Rename it to the config.key
        outputCatalog.index.rename(self.config.key, inplace=True)

        self.log.info("Made a table of %d columns and %d rows",
                      len(outputCatalog.columns), len(outputCatalog))
        return pipeBase.Struct(outputCatalog=outputCatalog)


class ConsolidateTractConnections(pipeBase.PipelineTaskConnections,
                                  defaultTemplates={"catalogType": ""},
                                  dimensions=("instrument", "tract")):
    inputCatalogs = connectionTypes.Input(
        doc="Input per-patch DataFrame Tables to be concatenated",
        name="{catalogType}ForcedSourceTable",
        storageClass="DataFrame",
        dimensions=("tract", "patch", "skymap"),
        multiple=True,
    )

    outputCatalog = connectionTypes.Output(
        doc="Output per-tract concatenation of DataFrame Tables",
        name="{catalogType}ForcedSourceTable_tract",
        storageClass="DataFrame",
        dimensions=("tract", "skymap"),
    )


class ConsolidateTractConfig(pipeBase.PipelineTaskConfig,
                             pipelineConnections=ConsolidateTractConnections):
    pass


class ConsolidateTractTask(pipeBase.PipelineTask):
    """Concatenate any per-patch, dataframe list into a single
    per-tract DataFrame.
    """
    _DefaultName = 'ConsolidateTract'
    ConfigClass = ConsolidateTractConfig

    def runQuantum(self, butlerQC, inputRefs, outputRefs):
        inputs = butlerQC.get(inputRefs)
        # Not checking at least one inputCatalog exists because that'd be an
        # empty QG.
        self.log.info("Concatenating %s per-patch %s Tables",
                      len(inputs['inputCatalogs']),
                      inputRefs.inputCatalogs[0].datasetType.name)
        df = pd.concat(inputs['inputCatalogs'])
        butlerQC.put(pipeBase.Struct(outputCatalog=df), outputRefs)
