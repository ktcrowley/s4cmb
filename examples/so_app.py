## This examples describes how to use some basic functionalities
## of s4cmb such as:
## * simulate an input sky
## * simulate an instrument
## * simulate a scanning strategy
## * simulate TOD from the 3 previous guys
## * project back your TOD to sky maps.
from __future__ import division, absolute_import, print_function

## Initialise MPI
from mpi4py import MPI

## Import modules and routines
from s4cmb.input_sky import HealpixFitsMap

from s4cmb.instrument import Hardware

from s4cmb.scanning_strategy import ScanningStrategy

from s4cmb.tod import TimeOrderedDataPairDiff
from s4cmb.tod import OutputSkyMap
from s4cmb.tod import partial2full

from s4cmb.config_s4cmb import NormaliseS4cmbParser

## Other packages needed
import os
import healpy as hp
import numpy as np
import argparse
import ConfigParser

try:
    from tqdm import *
except ImportError:
    def tqdm(x):
        """
        Do nothing. Just return x.
        """
        return x

def addargs(parser):
    ''' Parse command line arguments '''
    parser.add_argument(
        '-inifile', dest='inifile',
        required=True,
        help='Configuration file with parameter values.')
    parser.add_argument(
        '-inifile_xpure', dest='inifile_xpure',
        default=None,
        help='Configuration file with xpure parameter values.')


if __name__ == "__main__":
    """
    Launch the pipeline!
    """
    parser = argparse.ArgumentParser(
        description='MPI version of s4cmb')
    addargs(parser)
    args = parser.parse_args(None)

    Config = ConfigParser.ConfigParser()
    Config.read(args.inifile)
    params = NormaliseS4cmbParser(Config._sections['s4cmb'])

    rank = MPI.COMM_WORLD.rank
    size = MPI.COMM_WORLD.size

    ##################################################################
    ## START OF THE SIMULATION
    ## Flow is the following:
    ##   sky -> instrument -> scanning strategy ->
    ##      MAP2TOD -> (systematics) -> TOD2MAP
    ##################################################################
    ## Initialise our input maps
    sky_in = HealpixFitsMap(params.input_filename,
                            FWHM_in=params.FWHM_in,
                            nside_in=params.nside_in,
                            map_seed=params.map_seed,
                            do_pol=params.do_pol,
                            verbose=params.verbose,
                            no_ileak=params.no_ileak,
                            no_quleak=params.no_quleak)

    ## Initialise our instrument
    inst = Hardware(ncrate=params.ncrate,
                    ndfmux_per_crate=params.ndfmux_per_crate,
                    nsquid_per_mux=params.nsquid_per_mux,
                    npair_per_squid=params.npair_per_squid,
                    fp_size=params.fp_size,
                    FWHM=params.FWHM,
                    beam_seed=params.beam_seed,
                    projected_fp_size=params.projected_fp_size,
                    pm_name=params.pm_name,
                    type_HWP=params.type_HWP,
                    freq_HWP=params.freq_HWP,
                    angle_HWP=params.angle_HWP,
                    verbose=params.verbose)

    ## Initialize our scanning strategy
    scan = ScanningStrategy(nCES=params.nCES,
                            start_date=params.start_date,
                            telescope_longitude=params.telescope_longitude,
                            telescope_latitude=params.telescope_latitude,
                            telescope_elevation=params.telescope_elevation,
                            name_strategy=params.name_strategy,
                            sampling_freq=params.sampling_freq,
                            sky_speed=params.sky_speed,
                            ut1utc_fn=params.ut1utc_fn,
                            language=params.language)
    scan.run()

    ## Let's now generate our TOD from our input sky, instrument,
    ## and scanning strategy.
    if params.verbose:
        print("Proc [{}] doing scans".format(rank), range(
            rank, scan.nCES, size))

    for pos_CES, CESnumber in enumerate(range(rank, scan.nCES, size)):
        tod = TimeOrderedDataPairDiff(inst, scan, sky_in,
                                      CESnumber=CESnumber,
                                      nside_out=params.nside_out,
                                      width=params.width)

        ## Initialise map containers for each processor
        if pos_CES == 0:
            sky_out_tot = OutputSkyMap(nside=params.nside_out,
                                       obspix=tod.obspix)

        ## Scan input map to get TODs
        d = []
        for det in tqdm(range(inst.focal_plane.nbolometer)):
            d.append(tod.map2tod(det))

        ## Project TOD to maps
        tod.tod2map(np.array(d), sky_out_tot)

    MPI.COMM_WORLD.barrier()

    ## Coaddition over all processors.
    ## Note that all processors will then have the coadded data.
    ## If you want informations at the level of each CES (or group of),
    ## use instead:
    ## final_map = OutputSkyMap(nside=nside_out, obspix=tod.obspix)
    ## final_map.coadd_MPI(sky_out_tot, MPI=MPI)
    sky_out_tot.coadd_MPI(sky_out_tot, MPI=MPI)

    ## Check that output = input
    if rank == 0:
        sky_out = partial2full(sky_out_tot.get_I(),
                               sky_out_tot.obspix, sky_out_tot.nside,
                               fill_with=0.0)
        mask = sky_out != 0
        assert np.all(np.abs(sky_in.I[mask] - sky_out[mask]) < 1e-7), \
            ValueError("Output not equal to input!")

        sky_out = partial2full(sky_out_tot.get_QU()[0],
                               sky_out_tot.obspix, sky_out_tot.nside,
                               fill_with=0.0)
        mask = sky_out != 0
        assert np.all(np.abs(sky_in.Q[mask] - sky_out[mask]) < 1e-7), \
            ValueError("Output not equal to input!")

        sky_out = partial2full(sky_out_tot.get_QU()[1],
                               sky_out_tot.obspix, sky_out_tot.nside,
                               fill_with=0.0)
        mask = sky_out != 0
        assert np.all(np.abs(sky_in.U[mask] - sky_out[mask]) < 1e-7), \
            ValueError("Output not equal to input!")

        print("All OK! Greetings from processor 0!")

    if rank == 0:
        from s4cmb.xpure import write_maps_a_la_xpure
        from s4cmb.xpure import write_weights_a_la_xpure
        from s4cmb.xpure import create_batch
        from s4cmb.config_s4cmb import NormaliseXpureParser
        import commands
        ## Save data on disk into fits file for later use in xpure
        name_out = '{}_{}_{}'.format(params.tag,
                                     params.name_instrument,
                                     params.name_strategy)
        write_maps_a_la_xpure(sky_out_tot, name_out=name_out,
                              output_path='xpure/maps')
        write_weights_a_la_xpure(sky_out_tot, name_out=name_out,
                                 output_path='xpure/masks',
                                 epsilon=0.08, HWP=False)

        if args.inifile_xpure is not None:
            Config = ConfigParser.ConfigParser()
            Config.read(args.inifile_xpure)
            params_xpure = NormaliseXpureParser(Config._sections['xpure'])
            batch_file = '{}_{}_{}.batch'.format(
                params.tag,
                params.name_instrument,
                params.name_strategy)
            create_batch(batch_file, params, params_xpure)

            qsub = commands.getoutput('sbatch ' + batch_file)
            print(qsub)

    MPI.COMM_WORLD.barrier()