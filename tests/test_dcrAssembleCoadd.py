from __future__ import absolute_import, division, print_function
# This file is part of pipe_tasks.
#
# LSST Data Management System
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
# See COPYRIGHT file at the top of the source tree.
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
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.

from astropy import units as u
import numpy as np
import unittest

from lsst.afw.coord import Observatory, Weather
import lsst.afw.geom as afwGeom
import lsst.afw.image as afwImage
import lsst.afw.math as afwMath
from lsst.geom import arcseconds, degrees, radians
from lsst.meas.algorithms.testUtils import plantSources
import lsst.utils.tests
from lsst.pipe.tasks.dcrAssembleCoadd import DcrAssembleCoaddTask, DcrAssembleCoaddConfig


class DcrAssembleCoaddTestTask(lsst.utils.tests.TestCase, DcrAssembleCoaddTask):
    """A test case for the DCR-aware image coaddition algorithm.

    Attributes
    ----------
    bbox : lsst.afw.geom.box.Box2I
        Bounding box of the test model.
    bufferSize : int
        Distance from the inner edge of the bounding box to avoid placing test sources in the model images.
    config : lsst.pipe.tasks.dcrAssembleCoadd.DcrAssembleCoaddConfig
        Configuration parameters to initialize the task.
    filterInfo : lsst.afw.image.filter.Filter
        Dummy filter object for testing.
    mask : lsst.afw.image.mask
        Reference mask of the unshifted model.
    """

    def setUp(self):
        """Define the filters,  DCR attributes, and the image bounding box for the tests."""
        self.config = DcrAssembleCoaddConfig()
        lambdaEff = 476.31  # Use LSST g band values for the test.
        lambdaMin = 405
        lambdaMax = 552
        afwImage.utils.defineFilter("gTest", lambdaEff, lambdaMin=lambdaMin, lambdaMax=lambdaMax)
        self.filterInfo = afwImage.Filter("gTest")
        self.config.dcrNumSubfilters = 3
        self.bufferSize = 5
        badMaskPlanes = self.config.badMaskPlanes[:]
        badMaskPlanes.append("CLIPPED")
        xSize = 40
        ySize = 42
        x0 = 12345
        y0 = 67890
        self.bbox = afwGeom.Box2I(afwGeom.Point2I(x0, y0), afwGeom.Extent2I(xSize, ySize))

    def makeTestImages(self):
        """Make reproduceable PSF-convolved masked images for testing.

        Returns
        -------
        dcrModels : list of lsst.afw.image.maskedImageF
            A list of masked images, each containing the model for one subfilter.
        """
        seed = 5
        rng = np.random
        rng.seed(seed)
        psfSize = 2
        nSrc = 5
        noiseLevel = 5
        detectionSigma = 5.
        sourceSigma = 20.
        fluxRange = 2.
        x0, y0 = self.bbox.getBegin()
        xSize, ySize = self.bbox.getDimensions()
        xLoc = rng.random(nSrc)*(xSize - 2*self.bufferSize) + self.bufferSize + x0
        yLoc = rng.random(nSrc)*(ySize - 2*self.bufferSize) + self.bufferSize + y0
        dcrModels = []

        imageSum = np.zeros((ySize, xSize))
        for subfilter in range(self.config.dcrNumSubfilters):
            flux = (rng.random(nSrc)*(fluxRange - 1.) + 1.)*sourceSigma*noiseLevel
            sigmas = [psfSize for src in range(nSrc)]
            coordList = [[x, y, counts, sigma] for x, y, counts, sigma in zip(xLoc, yLoc, flux, sigmas)]
            model = plantSources(self.bbox, 10, 0, coordList, addPoissonNoise=False)
            model.image.array += rng.random((ySize, xSize))*noiseLevel
            imageSum += model.image.array
            model.mask.addMaskPlane("CLIPPED")
            dcrModels.append(model.maskedImage)
        maskVals = np.zeros_like(imageSum)
        maskVals[imageSum > detectionSigma*noiseLevel] = afwImage.Mask.getPlaneBitMask('DETECTED')
        for model in dcrModels:
            model.mask.array[:] = maskVals
        self.mask = dcrModels[0].mask
        return dcrModels

    def makeDummyWcs(self, rotAngle, pixelScale, crval):
        """Make a World Coordinate System object for testing.

        Parameters
        ----------
        rotAngle : `lsst.geom.Angle`
            rotation of the CD matrix, East from North
        pixelScale : `lsst.geom.Angle`
            Pixel scale of the projection.
        crval : lsst.afw.geom.SpherePoint
            Coordinates of the reference pixel of the wcs.

        Returns
        -------
        lsst.afw.geom.skyWcs.skyWcs.SkyWcs
            A wcs that matches the inputs.
        """
        crpix = afwGeom.Box2D(self.bbox).getCenter()
        cd_matrix = afwGeom.makeCdMatrix(scale=pixelScale, orientation=rotAngle, flipX=True)
        wcs = afwGeom.makeSkyWcs(crpix=crpix, crval=crval, cdMatrix=cd_matrix)
        return wcs

    def makeDummyVisitInfo(self, azimuth, elevation):
        """Make a self-consistent visitInfo object for testing.

        For simplicity, the simulated observation is assumed to be taken on the local meridian.

        Parameters
        ----------
        azimuth : `lsst.geom.Angle`
            Azimuth angle of the simulated observation.
        elevation : `lsst.geom.Angle`
            Elevation angle of the simulated observation.

        Returns
        -------
        lsst.afw.image.VisitInfo
            VisitInfo for the exposure.
        """
        lsstLat = -30.244639*degrees
        lsstLon = -70.749417*degrees
        lsstAlt = 2663.
        lsstTemperature = 20.*u.Celsius  # in degrees Celcius
        lsstHumidity = 40.  # in percent
        lsstPressure = 73892.*u.pascal
        lsstWeather = Weather(lsstTemperature.value, lsstPressure.value, lsstHumidity)
        lsstObservatory = Observatory(lsstLon, lsstLat, lsstAlt)
        airmass = 1.0/np.sin(elevation.asRadians())
        era = 0.*radians  # on the meridian
        zenithAngle = 90.*degrees - elevation
        ra = lsstLon + np.sin(azimuth.asRadians())*zenithAngle/np.cos(lsstLat.asRadians())
        dec = lsstLat + np.cos(azimuth.asRadians())*zenithAngle
        visitInfo = afwImage.VisitInfo(era=era,
                                       boresightRaDec=afwGeom.SpherePoint(ra, dec),
                                       boresightAzAlt=afwGeom.SpherePoint(azimuth, elevation),
                                       boresightAirmass=airmass,
                                       boresightRotAngle=0.*radians,
                                       observatory=lsstObservatory,
                                       weather=lsstWeather
                                       )
        return visitInfo

    def testDcrShiftCalculation(self):
        """Test that the shift in pixels due to DCR is consistently computed.

        The shift is compared to pre-computed values.
        """
        rotAngle = 0.*radians
        azimuth = 30.*degrees
        elevation = 65.*degrees
        pixelScale = 0.2*arcseconds
        visitInfo = self.makeDummyVisitInfo(azimuth, elevation)
        wcs = self.makeDummyWcs(rotAngle, pixelScale, crval=visitInfo.getBoresightRaDec())
        dcrShift = self.dcrShiftCalculate(visitInfo, wcs)
        refShift = [afwGeom.Extent2D(-0.5363512808, -0.3103517169),
                    afwGeom.Extent2D(0.001887293861, 0.001092054612),
                    afwGeom.Extent2D(0.3886592703, 0.2248919247)]
        for shiftOld, shiftNew in zip(refShift, dcrShift):
            self.assertFloatsAlmostEqual(shiftOld.getX(), shiftNew.getX(), rtol=1e-6, atol=1e-8)
            self.assertFloatsAlmostEqual(shiftOld.getY(), shiftNew.getY(), rtol=1e-6, atol=1e-8)

    def testRotationAngle(self):
        """Test that he sky rotation angle is consistently computed.

        The rotation is compared to pre-computed values.
        """
        cdRotAngle = 0.*radians
        azimuth = 130.*afwGeom.degrees
        elevation = 70.*afwGeom.degrees
        pixelScale = 0.2*afwGeom.arcseconds
        visitInfo = self.makeDummyVisitInfo(azimuth, elevation)
        wcs = self.makeDummyWcs(cdRotAngle, pixelScale, crval=visitInfo.getBoresightRaDec())
        rotAngle = self.calculateRotationAngle(visitInfo, wcs)
        refAngle = -0.9344289857053072*radians
        self.assertAnglesAlmostEqual(refAngle, rotAngle, maxDiff=1e-6*radians)

    def testConditionDcrModelNoChange(self):
        """Conditioning should not change the model if it is identical to the reference.

        This additionally tests that the variance and mask planes do not change.
        """
        dcrModels = self.makeTestImages()
        refModels = [model.clone() for model in dcrModels]
        self.conditionDcrModel(refModels, dcrModels, self.bbox, gain=1.)
        for model, refModel in zip(dcrModels, refModels):
            self.assertMaskedImagesEqual(model, refModel)

    def testConditionDcrModelWithChange(self):
        """Verify the effect of conditioning when the model changes by a known amount.

        This additionally tests that the variance and mask planes do not change.
        """
        dcrModels = self.makeTestImages()
        refModels = [model.clone() for model in dcrModels]
        for model in dcrModels:
            model.image.array[:] *= 3.
        self.conditionDcrModel(refModels, dcrModels, self.bbox, gain=1.)
        for model, refModel in zip(dcrModels, refModels):
            refModel.image.array[:] *= 2.
            self.assertMaskedImagesEqual(model, refModel)

    def testRegularizationLargeClamp(self):
        """Frequency regularization should leave the models unchanged if the clamp factor is large.

        This also tests that noise-like pixels are not regularized.
        """
        dcrModels = self.makeTestImages()
        self.config.regularizeSigma = 3.
        self.config.clampFrequency = 2.
        statsCtrl = afwMath.StatisticsControl()
        modelRefs = [model.clone() for model in dcrModels]
        self.regularizeModel(dcrModels, self.bbox, self.mask, statsCtrl)
        for model, modelRef in zip(dcrModels, modelRefs):
            self.assertMaskedImagesEqual(model, modelRef)

    def testRegularizationSmallClamp(self):
        """Test that large variations between model planes are reduced.

        This also tests that noise-like pixels are not regularized.
        """
        dcrModels = self.makeTestImages()
        self.config.regularizeSigma = 3.
        self.config.clampFrequency = 1.1
        statsCtrl = afwMath.StatisticsControl()
        modelRefs = [model.clone() for model in dcrModels]
        templateImage = np.sum([model.image.array for model in dcrModels], axis=0)
        backgroundInds = self.mask.array == 0
        noiseLevel = self.config.regularizeSigma*np.std(templateImage[backgroundInds])

        self.regularizeModel(dcrModels, self.bbox, self.mask, statsCtrl)
        for model, modelRef in zip(dcrModels, modelRefs):
            self.assertFloatsAlmostEqual(model.mask.array, modelRef.mask.array)
            self.assertFloatsAlmostEqual(model.variance.array, modelRef.variance.array)
            imageDiffHigh = model.image.array - (templateImage*self.config.clampFrequency + noiseLevel)
            self.assertLessEqual(np.max(imageDiffHigh), 0.)
            imageDiffLow = model.image.array - (templateImage/self.config.clampFrequency - noiseLevel)
            self.assertGreaterEqual(np.max(imageDiffLow), 0.)


class MyMemoryTestCase(lsst.utils.tests.MemoryTestCase):
    pass


def setup_module(module):
    lsst.utils.tests.init()


if __name__ == "__main__":
    lsst.utils.tests.init()
    unittest.main()
