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

import unittest
import tempfile

import astropy.units as u
from astropy.coordinates import SkyCoord
import numpy as np

import lsst.afw.image as afwImage
import lsst.afw.table as afwTable
import lsst.daf.base
import lsst.daf.butler.tests as butlerTests
import lsst.geom
import lsst.meas.algorithms
from lsst.meas.algorithms import testUtils
import lsst.meas.extensions.psfex
import lsst.meas.base.tests
import lsst.pipe.base.testUtils
from lsst.pipe.tasks.calibrateImage import CalibrateImageTask
import lsst.utils.tests


class CalibrateImageTaskTests(lsst.utils.tests.TestCase):

    def setUp(self):
        # Different x/y dimensions so they're easy to distinguish in a plot,
        # and non-zero minimum, to help catch xy0 errors.
        bbox = lsst.geom.Box2I(lsst.geom.Point2I(5, 4), lsst.geom.Point2I(205, 184))
        self.sky_center = lsst.geom.SpherePoint(245.0, -45.0, lsst.geom.degrees)
        self.photo_calib = 12.3
        dataset = lsst.meas.base.tests.TestDataset(bbox, crval=self.sky_center, calibration=self.photo_calib)
        # sqrt of area of a normalized 2d gaussian
        psf_scale = np.sqrt(4*np.pi*(dataset.psfShape.getDeterminantRadius())**2)
        noise = 10.0  # stddev of noise per pixel
        # Sources ordered from faintest to brightest.
        self.fluxes = np.array((6*noise*psf_scale,
                                12*noise*psf_scale,
                                45*noise*psf_scale,
                                150*noise*psf_scale,
                                400*noise*psf_scale,
                                1000*noise*psf_scale))
        self.centroids = np.array(((162, 22),
                                   (25, 70),
                                   (100, 160),
                                   (50, 120),
                                   (92, 35),
                                   (175, 154)), dtype=np.float32)
        for flux, centroid in zip(self.fluxes, self.centroids):
            dataset.addSource(instFlux=flux, centroid=lsst.geom.Point2D(centroid[0], centroid[1]))

        # Bright extended source in the center of the image: should not appear
        # in any of the output catalogs.
        center = lsst.geom.Point2D(100, 100)
        shape = lsst.afw.geom.Quadrupole(8, 9, 3)
        dataset.addSource(instFlux=500*noise*psf_scale, centroid=center, shape=shape)

        schema = dataset.makeMinimalSchema()
        self.truth_exposure, self.truth_cat = dataset.realize(noise=noise, schema=schema)
        # To make it look like a version=1 (nJy fluxes) refcat
        self.truth_cat = self.truth_exposure.photoCalib.calibrateCatalog(self.truth_cat)
        self.ref_loader = testUtils.MockReferenceObjectLoaderFromMemory([self.truth_cat])
        metadata = lsst.daf.base.PropertyList()
        metadata.set("REFCAT_FORMAT_VERSION", 1)
        self.truth_cat.setMetadata(metadata)

        # TODO: a cosmic ray (need to figure out how to insert a fake-CR)
        # self.truth_exposure.image.array[10, 10] = 100000
        # self.truth_exposure.variance.array[10, 10] = 100000/noise

        # Copy the truth exposure, because CalibrateImage modifies the input.
        # Post-ISR ccds only contain: initial WCS, VisitInfo, filter
        self.exposure = afwImage.ExposureF(self.truth_exposure.maskedImage)
        self.exposure.setWcs(self.truth_exposure.wcs)
        self.exposure.info.setVisitInfo(self.truth_exposure.visitInfo)
        # "truth" filter, to match the "truth" refcat.
        self.exposure.setFilter(lsst.afw.image.FilterLabel(physical='truth', band="truth"))

        # Test-specific configuration:
        self.config = CalibrateImageTask.ConfigClass()
        # We don't have many sources, so have to fit simpler models.
        self.config.psf_detection.background.approxOrderX = 1
        self.config.star_detection.background.approxOrderX = 1
        # Use PCA psf fitter, as psfex fails if there are only 4 stars.
        self.config.psf_measure_psf.psfDeterminer = 'pca'
        # We don't have many test points, so can't match on complicated shapes.
        self.config.astrometry.matcher.numPointsForShape = 3

        # Something about this test dataset prefers the older fluxRatio here.
        self.config.star_catalog_calculation.plugins['base_ClassificationExtendedness'].fluxRatio = 0.925

    def test_run(self):
        """Test that run() returns reasonable values to be butler put.
        """
        calibrate = CalibrateImageTask(config=self.config)
        calibrate.astrometry.setRefObjLoader(self.ref_loader)
        calibrate.photometry.match.setRefObjLoader(self.ref_loader)
        result = calibrate.run(exposure=self.exposure)

        # Background should have 4 elements: 3 from compute_psf and one from
        # re-estimation during source detection.
        self.assertEqual(len(result.background), 4)

        # Check that the summary statistics are reasonable.
        summary = self.exposure.info.getSummaryStats()
        self.assertFloatsAlmostEqual(self.exposure.info.getSummaryStats().psfSigma, 2.0, rtol=1e-2)
        self.assertFloatsAlmostEqual(summary.ra, self.sky_center.getRa().asDegrees(), rtol=1e-7)
        self.assertFloatsAlmostEqual(summary.dec, self.sky_center.getDec().asDegrees(), rtol=1e-7)

        # Returned photoCalib should be the applied value, not the ==1 one on the exposure.
        self.assertFloatsAlmostEqual(result.applied_photo_calib.getCalibrationMean(),
                                     self.photo_calib, rtol=2e-3)
        # Should have flux/magnitudes in the catalog.
        self.assertIn("slot_PsfFlux_flux", result.stars.schema)
        self.assertIn("slot_PsfFlux_mag", result.stars.schema)

        # Check that all necessary fields are in the output.
        lsst.pipe.base.testUtils.assertValidOutput(calibrate, result)

    def test_compute_psf(self):
        """Test that our brightest sources are found by _compute_psf(),
        that a PSF is assigned to the expopsure.
        """
        calibrate = CalibrateImageTask(config=self.config)
        sources, background, candidates = calibrate._compute_psf(self.exposure)

        # Background should have 3 elements: initial subtraction, and two from
        # re-estimation during the two detection passes.
        self.assertEqual(len(background), 3)

        # Only the point-sources with S/N > 50 should be in this output.
        self.assertEqual(sources["calib_psf_used"].sum(), 3)
        # Sort in order of brightness, to easily compare with expected positions.
        sources.sort(sources.getPsfFluxSlot().getMeasKey())
        for record, flux, center in zip(sources[::-1], self.fluxes, self.centroids[self.fluxes > 50]):
            self.assertFloatsAlmostEqual(record.getX(), center[0], rtol=0.01)
            self.assertFloatsAlmostEqual(record.getY(), center[1], rtol=0.01)
            # PsfFlux should match the values inserted.
            self.assertFloatsAlmostEqual(record["slot_PsfFlux_instFlux"], flux, rtol=0.01)

        # TODO: While debugging DM-32701, we're using PCA instead of psfex.
        # Check that we got a useable PSF.
        # self.assertIsInstance(self.exposure.psf, lsst.meas.extensions.psfex.PsfexPsf)
        self.assertIsInstance(self.exposure.psf, lsst.meas.algorithms.PcaPsf)
        # TestDataset sources have PSF radius=2 pixels.
        radius = self.exposure.psf.computeShape(self.exposure.psf.getAveragePosition()).getDeterminantRadius()
        self.assertFloatsAlmostEqual(radius, 2.0, rtol=1e-2)

        # To look at images for debugging (`setup display_ds9` and run ds9):
        # import lsst.afw.display
        # display = lsst.afw.display.getDisplay()
        # display.mtv(self.exposure)

    def test_measure_aperture_correction(self):
        """Test that _measure_aperture_correction() assigns an ApCorrMap to the
        exposure.
        """
        calibrate = CalibrateImageTask(config=self.config)
        sources, background, candidates = calibrate._compute_psf(self.exposure)

        # First check that the exposure doesn't have an ApCorrMap.
        self.assertIsNone(self.exposure.apCorrMap)
        calibrate._measure_aperture_correction(self.exposure, sources)
        self.assertIsInstance(self.exposure.apCorrMap, afwImage.ApCorrMap)

    def test_find_stars(self):
        """Test that _find_stars() correctly identifies the S/N>10 stars
        in the image and returns them in the output catalog.
        """
        calibrate = CalibrateImageTask(config=self.config)
        sources, background, candidates = calibrate._compute_psf(self.exposure)
        calibrate._measure_aperture_correction(self.exposure, sources)

        stars = calibrate._find_stars(self.exposure, background)

        # Background should have 4 elements: 3 from compute_psf and one from
        # re-estimation during source detection.
        self.assertEqual(len(background), 4)

        # Only psf-like sources with S/N>10 should be in the output catalog.
        self.assertEqual(len(stars), 4)
        self.assertTrue(sources.isContiguous())
        # Sort in order of brightness, to easily compare with expected positions.
        sources.sort(sources.getPsfFluxSlot().getMeasKey())
        for record, flux, center in zip(sources[::-1], self.fluxes, self.centroids[self.fluxes > 50]):
            self.assertFloatsAlmostEqual(record.getX(), center[0], rtol=0.01)
            self.assertFloatsAlmostEqual(record.getY(), center[1], rtol=0.01)
            self.assertFloatsAlmostEqual(record["slot_PsfFlux_instFlux"], flux, rtol=0.01)

    def test_astrometry(self):
        """Test that the fitted WCS gives good catalog coordinates.
        """
        calibrate = CalibrateImageTask(config=self.config)
        calibrate.astrometry.setRefObjLoader(self.ref_loader)
        sources, background, candidates = calibrate._compute_psf(self.exposure)
        calibrate._measure_aperture_correction(self.exposure, sources)
        stars = calibrate._find_stars(self.exposure, background)

        calibrate._fit_astrometry(self.exposure, stars)

        # Check that we got reliable matches with the truth coordinates.
        fitted = SkyCoord(stars['coord_ra'], stars['coord_dec'], unit="radian")
        truth = SkyCoord(self.truth_cat['coord_ra'], self.truth_cat['coord_dec'], unit="radian")
        idx, d2d, _ = fitted.match_to_catalog_sky(truth)
        np.testing.assert_array_less(d2d.to_value(u.milliarcsecond), 30.0)

    def test_photometry(self):
        """Test that the fitted photoCalib matches the one we generated,
        and that the exposure is calibrated.
        """
        calibrate = CalibrateImageTask(config=self.config)
        calibrate.astrometry.setRefObjLoader(self.ref_loader)
        calibrate.photometry.match.setRefObjLoader(self.ref_loader)
        sources, background, candidates = calibrate._compute_psf(self.exposure)
        calibrate._measure_aperture_correction(self.exposure, sources)
        stars = calibrate._find_stars(self.exposure, background)
        calibrate._fit_astrometry(self.exposure, stars)

        stars, matches, meta, photoCalib = calibrate._fit_photometry(self.exposure, stars)

        # NOTE: With this test data, PhotoCalTask returns calibrationErr==0,
        # so we can't check that the photoCal error has been set.
        self.assertFloatsAlmostEqual(photoCalib.getCalibrationMean(), self.photo_calib, rtol=2e-3)
        # The exposure should be calibrated by the applied photoCalib.
        self.assertFloatsAlmostEqual(self.exposure.image.array/self.truth_exposure.image.array,
                                     self.photo_calib, rtol=2e-3)
        # PhotoCalib on the exposure must be identically 1.
        self.assertEqual(self.exposure.photoCalib.getCalibrationMean(), 1.0)

        # Check that we got reliable magnitudes and fluxes vs. truth.
        fitted = SkyCoord(stars['coord_ra'], stars['coord_dec'], unit="radian")
        truth = SkyCoord(self.truth_cat['coord_ra'], self.truth_cat['coord_dec'], unit="radian")
        idx, _, _ = fitted.match_to_catalog_sky(truth)
        # Because the input variance image does not include contributions from
        # the sources, we can't use fluxErr as a bound on the measurement
        # quality here.
        self.assertFloatsAlmostEqual(stars['slot_PsfFlux_flux'], self.truth_cat['truth_flux'][idx], rtol=0.1)
        self.assertFloatsAlmostEqual(stars['slot_PsfFlux_mag'], self.truth_cat['truth_mag'][idx], rtol=0.01)


class CalibrateImageTaskRunQuantumTests(lsst.utils.tests.TestCase):
    """Tests of ``CalibrateImageTask.runQuantum``, which need a test butler,
    but do not need real images.
    """
    def setUp(self):
        instrument = "testCam"
        exposure = 101
        visit = 100101
        detector = 42

        # Create a and populate a test butler for runQuantum tests.
        self.repo_path = tempfile.TemporaryDirectory()
        self.repo = butlerTests.makeTestRepo(self.repo_path.name)

        # dataIds for fake data
        butlerTests.addDataIdValue(self.repo, "instrument", instrument)
        butlerTests.addDataIdValue(self.repo, "exposure", exposure)
        butlerTests.addDataIdValue(self.repo, "visit", visit)
        butlerTests.addDataIdValue(self.repo, "detector", detector)

        # inputs
        butlerTests.addDatasetType(self.repo, "postISRCCD", {"instrument", "exposure", "detector"},
                                   "ExposureF")
        butlerTests.addDatasetType(self.repo, "gaia_dr3_20230707", {"htm7"}, "SimpleCatalog")
        butlerTests.addDatasetType(self.repo, "ps1_pv3_3pi_20170110", {"htm7"}, "SimpleCatalog")

        # outputs
        butlerTests.addDatasetType(self.repo, "initial_pvi", {"instrument", "visit", "detector"},
                                   "ExposureF")
        butlerTests.addDatasetType(self.repo, "initial_stars_footprints_detector",
                                   {"instrument", "visit", "detector"},
                                   "SourceCatalog")
        butlerTests.addDatasetType(self.repo, "initial_photoCalib_detector",
                                   {"instrument", "visit", "detector"},
                                   "PhotoCalib")
        # optional outputs
        butlerTests.addDatasetType(self.repo, "initial_pvi_background", {"instrument", "visit", "detector"},
                                   "Background")
        butlerTests.addDatasetType(self.repo, "initial_psf_stars_footprints",
                                   {"instrument", "visit", "detector"},
                                   "SourceCatalog")
        butlerTests.addDatasetType(self.repo,
                                   "initial_astrometry_match_detector",
                                   {"instrument", "visit", "detector"},
                                   "Catalog")
        butlerTests.addDatasetType(self.repo,
                                   "initial_photometry_match_detector",
                                   {"instrument", "visit", "detector"},
                                   "Catalog")

        # dataIds
        self.exposure_id = self.repo.registry.expandDataId(
            {"instrument": instrument, "exposure": exposure, "detector": detector})
        self.visit_id = self.repo.registry.expandDataId(
            {"instrument": instrument, "visit": visit, "detector": detector})
        self.htm_id = self.repo.registry.expandDataId({"htm7": 42})

        # put empty data
        self.butler = butlerTests.makeTestCollection(self.repo)
        self.butler.put(afwImage.ExposureF(), "postISRCCD", self.exposure_id)
        self.butler.put(afwTable.SimpleCatalog(), "gaia_dr3_20230707", self.htm_id)
        self.butler.put(afwTable.SimpleCatalog(), "ps1_pv3_3pi_20170110", self.htm_id)

    def tearDown(self):
        del self.repo_path  # this removes the temporary directory

    def test_runQuantum(self):
        task = CalibrateImageTask()
        lsst.pipe.base.testUtils.assertValidInitOutput(task)

        quantum = lsst.pipe.base.testUtils.makeQuantum(
            task, self.butler, self.visit_id,
            {"exposure": self.exposure_id,
             "astrometry_ref_cat": [self.htm_id],
             "photometry_ref_cat": [self.htm_id],
             # outputs
             "output_exposure": self.visit_id,
             "stars": self.visit_id,
             "background": self.visit_id,
             "psf_stars": self.visit_id,
             "applied_photo_calib": self.visit_id,
             "initial_pvi_background": self.visit_id,
             "astrometry_matches": self.visit_id,
             "photometry_matches": self.visit_id,
             })
        mock_run = lsst.pipe.base.testUtils.runTestQuantum(task, self.butler, quantum)

        # Ensure the reference loaders have been configured.
        self.assertEqual(task.astrometry.refObjLoader.name, "gaia_dr3_20230707")
        self.assertEqual(task.photometry.match.refObjLoader.name, "ps1_pv3_3pi_20170110")
        # Check that the proper kwargs are passed to run().
        self.assertEqual(mock_run.call_args.kwargs.keys(), {"exposure"})

    def test_runQuantum_no_optional_outputs(self):
        config = CalibrateImageTask.ConfigClass()
        config.optional_outputs = None
        task = CalibrateImageTask(config=config)
        lsst.pipe.base.testUtils.assertValidInitOutput(task)

        quantum = lsst.pipe.base.testUtils.makeQuantum(
            task, self.butler, self.visit_id,
            {"exposure": self.exposure_id,
             "astrometry_ref_cat": [self.htm_id],
             "photometry_ref_cat": [self.htm_id],
             # outputs
             "output_exposure": self.visit_id,
             "stars": self.visit_id,
             "applied_photo_calib": self.visit_id,
             "background": self.visit_id,
             })
        mock_run = lsst.pipe.base.testUtils.runTestQuantum(task, self.butler, quantum)

        # Ensure the reference loaders have been configured.
        self.assertEqual(task.astrometry.refObjLoader.name, "gaia_dr3_20230707")
        self.assertEqual(task.photometry.match.refObjLoader.name, "ps1_pv3_3pi_20170110")
        # Check that the proper kwargs are passed to run().
        self.assertEqual(mock_run.call_args.kwargs.keys(), {"exposure"})

    def test_lintConnections(self):
        """Check that the connections are self-consistent.
        """
        Connections = CalibrateImageTask.ConfigClass.ConnectionsClass
        lsst.pipe.base.testUtils.lintConnections(Connections)


def setup_module(module):
    lsst.utils.tests.init()


class MemoryTestCase(lsst.utils.tests.MemoryTestCase):
    pass


if __name__ == "__main__":
    lsst.utils.tests.init()
    unittest.main()
