import math

import pandas as pd
from gnss_lib_py.utils.time_conversions import (
    datetime_to_tow,
    tow_to_datetime,
    get_leap_seconds,
)
from gnss_lib_py.utils.sim_gnss import find_sat
import numpy as np
from pathlib import Path
from datetime import datetime
import georinex as gr
import process_ephemerides as eph
import zipfile

import helpers
import converters
import parse_rinex
import constants
import shutil
import pytest
import os


# This function sets up a temporary directory, copies a zipped rinex navigation file into that directory
# and returns its path. The @pytest.fixture annotation allows us to pass the function as an input
# to test functions. When running a test function, pytest will then first run this function, pass
# whatever is passed to `yield` to the test function, and run the code after `yield` after the test,
# even  if the test crashes.
@pytest.fixture
def input_for_test():
    test_directory = Path(f"./tmp_test_directory_{__name__}").resolve()
    if test_directory.exists():
        # Start from empty directory, might avoid hiding some subtle bugs, e.g.
        # file decompression not working properly
        shutil.rmtree(test_directory)
    os.makedirs(test_directory)
    gps_rnx3_nav_test_file = test_directory.joinpath("BRDC00IGS_R_20220010000_01D_GN.zip")
    shutil.copy(
        helpers.prx_root().joinpath(
            f"datasets/TLSE_2022001/{gps_rnx3_nav_test_file.name}"
        ),
        gps_rnx3_nav_test_file,
    )
    assert gps_rnx3_nav_test_file.exists()

    all_constellations_rnx3_nav_test_file = test_directory.joinpath("BRDC00IGS_R_20220010000_01D_MN.zip")
    shutil.copy(
        helpers.prx_root().joinpath(
            f"datasets/TLSE_2022001/{all_constellations_rnx3_nav_test_file.name}"
        ),
        all_constellations_rnx3_nav_test_file,
    )
    assert all_constellations_rnx3_nav_test_file.exists()

    yield {"gps_nav_file": gps_rnx3_nav_test_file, "all_constellations_nav_file": all_constellations_rnx3_nav_test_file}
    shutil.rmtree(test_directory)


def test_compare_rnx3_sat_pos_with_magnitude(input_for_test):
    """Loads a RNX3 file, compute a position for different satellites and time, and compare to MAGNITUDE results
    Test will be a success if the difference in position is lower than threshold_pos_error_m = 0.01
    """
    path_to_rnx3_nav_file = converters.anything_to_rinex_3(input_for_test["gps_nav_file"])

    threshold_pos_error_m = 0.01

    # select sv and time
    sv = np.array("G01", dtype="<U3")
    gps_week = 2190
    gps_tow = 523800

    # MAGNITUDE position
    sv_pos_magnitude = np.array([13053451.235, -12567273.060, 19015357.126])

    # Compute broadcast satellite position
    # Select right ephemeris
    ephemerides = eph.convert_rnx3_nav_file_to_dataframe(path_to_rnx3_nav_file)
    date = np.datetime64(tow_to_datetime(gps_week, gps_tow))
    sv_posvel = eph.compute_satellite_state(ephemerides, sv, date)
    sv_pos_rnx3 = np.array(
        [
            sv_posvel["x"].values[0],
            sv_posvel["y"].values[0],
            sv_posvel["z"].values[0],
        ]
    )

    assert np.linalg.norm(sv_pos_rnx3 - sv_pos_magnitude) < threshold_pos_error_m


def test_compute_satellite_clock_offset(input_for_test):
    # GPS, GAL, QZSS, BDS, IRNSS broadcast satellite clock system time offsets are all given
    # as parameters of a polynomial of order 2, so this test should cover those constellations.
    # When computing the satellite clock offset of GPS-001 for January 1st 2022 at 1am GPST
    # We expect the clock offset to be computed from the following RINEX 3 ephemeris
    """
    G01 2022 01 01 00 00 00 4.691267386079e-04-1.000444171950e-11 0.000000000000e+00
         3.900000000000e+01-1.411250000000e+02 3.988380417768e-09-6.242942382352e-01
        -7.363036274910e-06 1.121813920327e-02 4.695728421211e-06 5.153674995422e+03
         5.184000000000e+05-3.166496753693e-08-1.036611240093e+00 1.955777406693e-07
         9.864187694897e-01 2.997500000000e+02 8.840876015687e-01-8.133553080847e-09
        -3.778728827795e-10 1.000000000000e+00 2.190000000000e+03 0.000000000000e+00
         2.000000000000e+00 0.000000000000e+00 5.122274160385e-09 3.900000000000e+01
         5.171890000000e+05 4.000000000000e+00 0.000000000000e+00 0.000000000000e+00
    """
    # copied from the following file
    rinex_3_navigation_file = converters.anything_to_rinex_3(input_for_test["gps_nav_file"])
    (
        computed_offset_m,
        computed_offset_rate_mps,
    ) = eph.compute_satellite_clock_offset_and_clock_offset_rate(
        eph.convert_rnx3_nav_file_to_dataframe(rinex_3_navigation_file),
        "G01",
        pd.Timestamp(np.datetime64("2022-01-01T01:00:00.000000000")),
    )
    # We expect the following clock offset and clock offset rate computed by hand from the parameters above.
    delta_t_s = constants.cSecondsPerHour
    expected_offset_m = constants.cGpsIcdSpeedOfLight_mps * (
        4.691267386079e-04
        + (-1.000444171950e-11 * delta_t_s)
        + 0.000000000000e00 * math.pow(delta_t_s, 2)
    )
    expected_offset_rate_mps = constants.cGpsIcdSpeedOfLight_mps * (-1.000444171950e-11 + 2 * 0.000000000000e00 * delta_t_s)
    # Expect micrometers and micrometers/s accuracy here:
    assert abs(expected_offset_m - computed_offset_m) < 1e-6
    assert abs(expected_offset_rate_mps - computed_offset_rate_mps) < 1e-6


def test_compute_satellite_clock_offset_glonass(input_for_test):
    # Glonass broadcast system time clock offsets are given as a clock offset in seconds
    # plus a relative frequency offset.
    # When computing the satellite clock offset of Glonass-001 for January 1st 2022 at 1am GLONASST
    # We expect the clock offset to be computed from the following RINEX 3 ephemeris
    """
    R01 2022 01 01 00 45 00 7.305294275284E-06-0.000000000000E+00 5.202000000000E+05
         1.799304101562E+04-1.798223495483E+00 1.862645149231E-09 0.000000000000E+00
         1.165609716797E+04-5.995044708252E-01-3.725290298462E-09 1.000000000000E+00
         1.381343408203E+04 2.848098754883E+00 0.000000000000E+00 0.000000000000E+00
    """
    # copied from the following file
    rinex_3_navigation_file = converters.anything_to_rinex_3(input_for_test["all_constellations_nav_file"])
    (
        computed_offset_m,
        computed_offset_rate_mps,
    ) = eph.compute_satellite_clock_offset_and_clock_offset_rate(
        eph.convert_rnx3_nav_file_to_dataframe(rinex_3_navigation_file),
        "R01",
        pd.Timestamp(np.datetime64("2022-01-01T01:00:00.000000000")),
    )
    # We expect the following clock offset and clock offset rate computed by hand from the parameters above.
    delta_t_s = constants.cSecondsPerHour
    expected_offset_m = constants.cGpsIcdSpeedOfLight_mps * (
        7.305294275284e-06 + (0.0 * delta_t_s) + math.pow(0.000000000000e00, 2)
    )
    expected_offset_rate_mps = 0
    # Expect micrometers and micrometers/s accuracy here:
    assert (
        abs(expected_offset_m - computed_offset_m) < 1e-6
    )
    assert (
        abs(expected_offset_rate_mps - computed_offset_rate_mps) < 1e-6
    )
