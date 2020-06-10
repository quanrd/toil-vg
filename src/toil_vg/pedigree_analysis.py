#!/usr/bin/env python
"""
pedigree_analysis.py: run the UDP analysis workflow on a pedigree processed by the 'toil-vg pedigree' subcommand workflow.

"""

import argparse, sys, os, os.path, errno, random, subprocess, shutil, itertools, glob, tarfile
import doctest, re, json, collections, time, timeit
import logging, logging.handlers, struct, socket, threading
import string
import getpass
import pdb
import gzip
import logging

from math import ceil
from subprocess import Popen, PIPE

from toil.common import Toil
from toil.job import Job
from toil.realtimeLogger import RealtimeLogger
from toil_vg.vg_common import *
from toil_vg.context import Context, run_write_info_to_outstore

logger = logging.getLogger(__name__)

def analysis_subparser(parser):
    """
    Create a subparser for the analysis workflow.  Should pass in results of subparsers.add_parser()
    """

    # Add the Toil options so the job store is the first argument
    Job.Runner.addToilOptions(parser)
    
    # General options
    parser.add_argument("out_store",
        help="output store.  All output written here. Path specified using same syntax as toil jobStore")
    # VCFtoShebang options
    parser.add_argument("--cohort_vcf", type=make_url, required=True,
        help="Path to cohort vcf processed by toil-vg pedigree")    
    parser.add_argument("--sample_name", type=str, required=True,
        help="Cohort proband sample name as present in the sample column of --cohort_vcf")
    parser.add_argument("--bypass", action="store_true", default=False
        help="Parameter for vcftoshebang.")
    # TODO: what is the bypass parameter?
    parser.add_argument("--cadd_lines", type=int, default=4985
        help="Parameter for vcftoshebang.")
    # TODO: what is the cadd lines parameter?
    parser.add_argument("--chrom_dir", type=str, required=True,
        help="Path to chromosome annotation directory used by vcftoshebang")
    parser.add_argument("--edit_dir", type=str, required=True,
        help="Path to directory containing master edit files used by vcftoshebang")
    
    # CADD options
    parser.add_argument("--split_lines", type=int, default=30000
        help="Number of lines to chunk the input VCF for CADD processing.")
    parser.add_argument("--genome_build", type=str, default="GRCh37",
        help="Genome annotation version for the CADD engine")
    parser.add_argument("--cadd_data", type=str, required=True,
        help="Path to cadd engine data directory")
    
    # BMTB options
    parser.add_argument("--maternal_bam", type=make_url, required=True,
        help="Input maternal bam file")    
    parser.add_argument("--maternal_bai", type=make_url, required=True,
        help="Input maternal bam file .bai index")    
    parser.add_argument("--paternal_bam", type=make_url, required=True,
        help="Input paternal bam file")    
    parser.add_argument("--paternal_bai", type=make_url, required=True,
        help="Input paternal bam file .bai index")    
    parser.add_argument("--siblings_bam", nargs='+', type=make_url, required=True,
        help="INPUT sibling bam files. Proband bam file must be listed 1st.")
    parser.add_argument("--siblings_bai", nargs='+', type=make_url, required=True,
        help="INPUT sibling bam file .bai indicies. Same sample order as --siblings_bam.")
    parser.add_argument("--maternal_name", type=str, required=True,
        help="Sample name of the pedigree mother.")
    parser.add_argument("--paternal_name", type=str, required=True,
        help="Sample name of the pedigree father.")
    parser.add_argument("--sibling_names", nargs='+', type=str, required=True,
        help="Sample names of the siblings. Same sample order as --siblings_bam.")
    parser.add_argument("--sibling_genders", nargs='+', type=int, required=True,
        help="Gender of each sibling sample. 0 = male, 1 = female. Same sample order as --siblings_bam.")
    parser.add_argument("--sibling_affected", nargs='+', type=int, required=True,
        help="Affected status of each sibling sample. 0 = unaffected, 1 = affected. Same sample order as --siblings_bam.")
    
    # Add common options shared with everybody
    add_common_vg_parse_args(parser)

    # Add common docker options
    add_container_tool_parse_args(parser)

    
def run_surjecting(job, context, gam_input_reads_id, output_name, interleaved, xg_file_id, paths):
    """ split the fastq, then surject each chunk.  returns outputgams, paired with total surject time
    (excluding toil-vg overhead such as transferring and splitting files )"""

    # to encapsulate everything under this job
    child_job = Job()
    job.addChild(child_job)

    if not context.config.single_reads_chunk:
        reads_chunk_ids = child_job.addChildJobFn(run_split_reads, context, None, 'aln.gam', None,
                                                  [gam_input_reads_id],
                                                  cores=context.config.misc_cores, memory=context.config.misc_mem,
                                                  disk=context.config.misc_disk).rv()
    else:
        RealtimeLogger.info("Bypassing reads splitting because --single_reads_chunk enabled")
        reads_chunk_ids = [[r] for r in [gam_input_reads_id]]

    return child_job.addFollowOnJobFn(run_whole_surject, context, reads_chunk_ids, output_name, 
                                      interleaved, xg_file_id, paths, cores=context.config.misc_cores,
                                      memory=context.config.misc_mem, disk=context.config.misc_disk).rv()
    
def run_whole_surject(job, context, reads_chunk_ids, output_name, interleaved, xg_file_id, paths):
    """
    Surject all gam chunks in parallel.
    
    surject all the GAM file IDs in read_chunk_ids, saving the merged BAM as output_name.
    
    If interleaved is true, expects paired-interleaved GAM input and writes paired BAM output.
    
    Surjects against the given collection of paths in the given XG file.
    
    """
    
    RealtimeLogger.info("Surjecting read chunks {} to BAM".format(reads_chunk_ids))
    
    # this will be a list of lists.
    # bam_chunk_file_ids[i][j], will correspond to the jth path (from id_ranges)
    # for the ith gam chunk (generated from fastq shard i)
    bam_chunk_file_ids = []
    bam_chunk_running_times = []

    # to encapsulate everything under this job
    child_job = Job()
    job.addChild(child_job)

    for chunk_id, chunk_filename_ids in enumerate(zip(*reads_chunk_ids)):
        #Run graph surject on each gam chunk
        chunk_surject_job = child_job.addChildJobFn(run_chunk_surject, context, interleaved, xg_file_id,
                                                    paths, chunk_filename_ids, '{}_chunk{}'.format(output_name, chunk_id),
                                                    cores=context.config.alignment_cores,
                                                    memory=context.config.alignment_mem,
                                                    disk=context.config.alignment_disk)
        bam_chunk_file_ids.append(chunk_surject_job.rv(0))
        bam_chunk_running_times.append(chunk_surject_job.rv(1))

    return child_job.addFollowOnJobFn(run_merge_bams, context, output_name, bam_chunk_file_ids,
                                      cores=context.config.misc_cores,
                                      memory=context.config.misc_mem, disk=context.config.misc_disk).rv()


def run_chunk_surject(job, context, interleaved, xg_file_id, paths, chunk_filename_ids, chunk_id):
    """ run surject on a chunk.  interface mostly copied from run_chunk_alignment.
    
    Takes an xg file and path colleciton to surject against, a list of chunk
    file IDs (must be just one possibly-interleaved chunk for now), and an
    identifying name/number/string (chunk_id) for the chunk.
    
    If interleaved is true, expects paired-interleaved GAM input and writes paired BAM output.
    
    Returns a single-element list of the resulting BAM file ID, and the run time in seconds.
    
    """

    # we can toggle this off if dual gams ever get supported by surject (unlikely)
    assert len(chunk_filename_ids) == 1
    
    # How long did the alignment take to run, in seconds?
    run_time = None
    
    # Define work directory for docker calls
    work_dir = job.fileStore.getLocalTempDir()

    xg_file = os.path.join(work_dir, "index.xg")
    job.fileStore.readGlobalFile(xg_file_id, xg_file)

    gam_files = []
    reads_ext = 'gam'
    for j, chunk_filename_id in enumerate(chunk_filename_ids):
        gam_file = os.path.join(work_dir, 'reads_chunk_{}_{}.{}'.format(chunk_id, j, reads_ext))
        job.fileStore.readGlobalFile(chunk_filename_id, gam_file)
        gam_files.append(gam_file)
    
    # And a temp file for our surject output
    output_file = os.path.join(work_dir, "surject_{}.bam".format(chunk_id))

    # Open the file stream for writing
    with open(output_file, 'wb') as surject_file:

        cmd = ['vg', 'surject', os.path.basename(gam_files[0]), '--bam-output']
        if interleaved:
            cmd += ['--interleaved']
        cmd += ['-x', os.path.basename(xg_file)]
        for surject_path in paths:
            cmd += ['--into-path', surject_path]
        cmd += ['-t', str(context.config.alignment_cores)]
        
        # Mark when we start the surjection
        start_time = timeit.default_timer()
        try:
            context.runner.call(job, cmd, work_dir = work_dir, outfile=surject_file)
        except:
            # Dump everything we need to replicate the surjection
            logging.error("Surjection failed. Dumping files.")
            context.write_output_file(job, xg_file)
            for gam_file in gam_files:
                context.write_output_file(job, gam_file)
            
            raise
        
        # Mark when it's done
        end_time = timeit.default_timer()
        run_time = end_time - start_time
        
    return [context.write_intermediate_file(job, output_file)], run_time

def run_merge_bams(job, context, output_name, bam_chunk_file_ids):
    """
    Merge together bams.
    
    Takes a list of lists of BAM file IDs to merge.
    """
    
    # First flatten the list of lists
    flat_ids = [x for l in bam_chunk_file_ids for x in l]
    
    # How much disk do we think we will need to have the merged and unmerged copies of these BAMs?
    # Make sure we have it
     
    requeue_promise = ensure_disk(job, run_merge_bams, [context, output_name, bam_chunk_file_ids], {},
        flat_ids, factor=2)
    if requeue_promise is not None:
        # We requeued ourselves with more disk to accomodate our inputs
        return requeue_promise
    
        # Otherwise, we have enough disk

    # Define work directory for docker calls
    work_dir = job.fileStore.getLocalTempDir()

    # Download our chunk files
    chunk_paths = [os.path.join(work_dir, 'chunk_{}.bam'.format(i)) for i in range(len(flat_ids))]
    for i, bam_chunk_file_id in enumerate(flat_ids):
        job.fileStore.readGlobalFile(bam_chunk_file_id, chunk_paths[i])

    # todo: option to give name
    surject_path = os.path.join(work_dir, '{}.bam'.format(output_name))

    cmd = ['samtools', 'cat'] + [os.path.basename(chunk_path) for chunk_path in chunk_paths]
    cmd += ['-o', os.path.basename(surject_path)]

    context.runner.call(job, cmd, work_dir = work_dir)

    return context.write_output_file(job, surject_path)
    
def analysis_main(context, options):
    """
    Wrapper for vg surject. 
    """

    # How long did it take to run the entire pipeline, in seconds?
    run_time_pipeline = None
        
    # Mark when we start the pipeline
    start_time_pipeline = timeit.default_timer()

    with context.get_toil(options.jobStore) as toil:
        if not toil.options.restart:

            importer = AsyncImporter(toil)
            
            # Upload local files to the remote IO Store
            inputXGFileID = importer.load(options.xg_index)
            inputGAMFileID = importer.load(options.gam_input_reads)

            importer.wait()

            # Make a root job
            root_job = Job.wrapJobFn(run_surjecting, context, importer.resolve(inputGAMFileID), 'surject',
                                     options.interleaved,
                                     importer.resolve(inputXGFileID), options.paths,
                                     cores=context.config.misc_cores,
                                     memory=context.config.misc_mem,
                                     disk=context.config.misc_disk)

            # Init the outstore
            init_job = Job.wrapJobFn(run_write_info_to_outstore, context, sys.argv,
                                     memory=context.config.misc_mem,
                                     disk=context.config.misc_disk)
            init_job.addFollowOn(root_job)            
            
            # Run the job and store the returned list of output files to download
            toil.start(init_job)
        else:
            toil.restart()
            
    end_time_pipeline = timeit.default_timer()
    run_time_pipeline = end_time_pipeline - start_time_pipeline
 
    logger.info("All jobs completed successfully. Pipeline took {} seconds.".format(run_time_pipeline))
    
