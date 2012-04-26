#!/usr/bin/env python
#
# LSST Data Management System
# Copyright 2008, 2009, 2010 LSST Corporation.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <http://www.lsstcorp.org/LegalNotices/>.
#
import lsst.pex.config as pexConfig
import lsst.pipe.base as pipeBase
import lsst.daf.base as dafBase
import lsst.afw.table as afwTable
from lsst.meas.algorithms import SourceDetectionTask, SourceMeasurementTask
from lsst.ip.isr import IsrTask
from lsst.pipe.tasks.calibrate import CalibrateTask

class ProcessCcdConfig(pexConfig.Config):
    """Config for ProcessCcd"""
    doIsr = pexConfig.Field(dtype=bool, default=True, doc = "Perform ISR?")
    doCalibrate = pexConfig.Field(dtype=bool, default=True, doc = "Perform calibration?")
    doDetection = pexConfig.Field(dtype=bool, default=True, doc = "Detect sources?")
    doMeasurement = pexConfig.Field(dtype=bool, default=True, doc = "Measure sources?")
    doWriteCalibrate = pexConfig.Field(dtype=bool, default=True, doc = "Write calibration results?")
    doWriteSources = pexConfig.Field(dtype=bool, default=True, doc = "Write sources?")
    isr = pexConfig.ConfigurableField(
        target = IsrTask,
        doc = "Instrumental Signature Removal",
    )
    calibrate = pexConfig.ConfigurableField(
        target = CalibrateTask,
        doc = "Calibration (inc. high-threshold detection and measurement)",
    )
    detection = pexConfig.ConfigurableField(
        target = SourceDetectionTask,
        doc = "Low-threshold detection for final measurement",
    )
    measurement = pexConfig.ConfigurableField(
        target = SourceMeasurementTask,
        doc = "Final source measurement on low-threshold detections",
    )

    def validate(self):
        pexConfig.Config.validate(self)
        if self.doMeasurement and not self.doDetection:
            raise ValueError("Cannot run source measurement without source detection.")

class ProcessCcdTask(pipeBase.CmdLineTask):
    """Process a CCD
    
    Available steps include:
    - instrument signature removal (ISR)
    - calibrate
    - detect sources
    - measure sources
    """
    ConfigClass = ProcessCcdConfig
    _DefaultName = "processCcd"

    def __init__(self, **kwargs):
        pipeBase.Task.__init__(self, **kwargs)
        self.makeSubtask("isr")
        self.makeSubtask("calibrate")
        self.schema = afwTable.SourceTable.makeMinimalSchema()
        self.algMetadata = dafBase.PropertyList()
        if self.config.doDetection:
            self.makeSubtask("detection", schema=self.schema)
        if self.config.doMeasurement:
            self.makeSubtask("measurement", schema=self.schema, algMetadata=self.algMetadata)

    @pipeBase.timeMethod
    def run(self, sensorRef):
        """Process one CCD
        
        @param sensorRef: sensor-level butler data reference
        @return pipe_base Struct containing these fields:
        - visitExposure: exposure after ISR performed if calib.doIsr or config.doCalibrate, else None
        - exposure: calibrated exposure (calexp) if config.doCalibrate or config.doDetection, else None
        - calib: object returned by calibration process if config.doCalibrate, else None
        - sources: detected source if config.doPhotometry, else None
        """
        self.log.log(self.log.INFO, "Processing %s" % (sensorRef.dataId))
        
        # initialize outputs
        visitExposure = None
        calExposure = None
        calib = None
        source = None
        
        if self.config.doIsr:
            visitExposure = self.isr.run(sensorRef).exposure

        if self.config.doCalibrate:
            if visitExposure is None:
                visitExposure = sensorRef.get('visitCCD')
            calib = self.calibrate.run(visitExposure)
            calExposure = calib.exposure
            if self.config.doWriteCalibrate:
                sensorRef.put(calExposure, 'calexp')
                sensorRef.put(calib.sources, 'icSrc')
                if calib.psf is not None:
                    sensorRef.put(calib.psf, 'psf')
                if calib.apCorr is not None:
                    sensorRef.put(calib.apCorr, 'apCorr')
                if calib.matches is not None:
                    normalizedMatches = afwTable.packMatches(calib.matches)
                    normalizedMatches.table.setMetadata(calib.matchMeta)
                    sensorRef.put(normalizedMatches, 'icMatch')

        if self.config.doDetection:
            if calExposure is None:
                calExposure = sensorRef.get('calexp')
            if calib is None:
                psf = sensorRef.get('psf')
                calExposure.setPsf(sensorRef.get('psf'))
            table = afwTable.SourceTable.make(self.schema)
            table.setMetadata(self.algMetadata)
            sources = self.detection.makeSourceCatalog(table, calExposure).sources
            if self.config.doWriteSources:
                sensorRef.put(sources, 'src')

        if self.config.doMeasurement:
            assert(sources is not None)
            assert(calExposure is not None)
            if calib is None:
                apCorr = sensorRef.get("apCorr")
            else:
                apCorr = calib.apCorr
            self.measurement.run(calExposure, sources, apCorr)
            
        return pipeBase.Struct(
            visitExposure = visitExposure
            exposure = calExposure,
            calib = calib,
            sources = sources,
        )
