import logging
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import filecmp
import shutil
from contextlib import closing
from unittest import TestCase, skip
from urlparse import urlparse
from uuid import uuid4
import urllib2, gzip

import os, sys
import posixpath

import pytest

from toil_vg.iostore import IOStore

log = logging.getLogger(__name__)


class VGCGLTest(TestCase):
    """
    Test toil-vg to make sure it is working correctly on some fixed input data.
    """

    @classmethod
    def setUpClass(cls):
        super(VGCGLTest, cls).setUpClass()
        # FIXME: pull up into common base class
        logging.basicConfig(level=logging.INFO)

    def _ci_input_path(self, filename):
        """
        Get the URL from which an input file can be obtained.
        """
        return 'https://{}.s3.amazonaws.com/{}/{}'.format(self.bucket_name, self.folder_name, filename)
    
    def _download_input(self, filename, local_path = None):
        # Where should we put this input file?
        tgt = os.path.join(self.workdir, filename) if local_path is None else local_path
        # And where does it come from?
        url = self._ci_input_path(filename)
        print(url)
        with open(tgt, 'w') as f:
            # Download the file from the URL
            connection = urllib2.urlopen(url)
            shutil.copyfileobj(connection, f)

    def setUp(self):
        # Set this to True to poke around in the outsores for debug purposes
        self.saveWorkDir = False
        self.workdir = './toil-vgci_work' if self.saveWorkDir else tempfile.mkdtemp()
        if not os.path.exists(self.workdir):
            os.makedirs(self.workdir)
        self.jobStoreLocal = '{}/local-testvg-{}'.format(self.workdir, uuid4())
        
        # Determine if we can use Docker or not
        self.containerType = os.environ.get('TOIL_VG_TEST_CONTAINER', 'Docker')

        # input files all in same bucket folder, which is specified (only) here:
        self.bucket_name = 'vg-data'
        self.folder_name = 'toil_vg_ci'
        
        self.base_command = ['toil-vg', 'run',
                             '--container', self.containerType,
                             '--realTimeLogging', '--logInfo', '--reads_per_chunk', '8000',
                             '--call_chunk_size', '20000',
                             '--gcsa_index_cores', '8',
                             '--alignment_cores', '4',
                             '--calling_cores', '4', '--vcfeval_cores', '4',
                             '--vcfeval_opts', ' --ref-overlap',
                             '--call_opts', '-E 0']
        
        # default output store
        self.local_outstore = os.path.join(self.workdir, 'toilvg-jenkinstest-outstore-{}'.format(uuid4()))

        # TODO: Since this might be on NFS, and sicne Toil can't clean up a job
        # store on NFS when the job finishes due to
        # https://github.com/DataBiosphere/toil/issues/2162, we need to pass
        # '--clean', 'never' to all our Toil workflows. We the test harness will clean up after them.

    def test_01_sim_small(self):
        ''' 
        This test uses simulated reads from the small dataset from vg, created as follows:
        vg construct -r test/small/x.fa -v test/small/x.vcf.gz > small.vg
        vg index -x small.xg small.vg
        vg sim -x small.xg  -l 100 -n 10000 -s 0 -e 0.001 -i 0.0001 > small_sim_reads
        # Reads converted to small_sim_reads.fq with script setting qualities to B
        (small.vcf.gz and small.fa.gz below are just x.vcf.gz and x.fa from input)
        '''
        self.sample_reads = self._ci_input_path('small_sim_reads.fq.gz')
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')

        self._run(self.base_command +
                  [self.jobStoreLocal, '1',
                   self.local_outstore, '--clean', 'never',
                   '--fastq', self.sample_reads,
                   '--graphs', self.test_vg_graph,
                   '--chroms', 'x', '--vcfeval_baseline', self.baseline,
                   '--vcfeval_fasta', self.chrom_fa, '--vcfeval_opts', ' --squash-ploidy'])
        self._run(['toil', 'clean', self.jobStoreLocal])
        
        self._assertOutput('1', self.local_outstore, f1_threshold=0.95)

    def test_02_sim_small_standalone(self):
        ''' 
        Same as above, but chain standalone tools instead of toil-vg run
        '''
        self.sample_reads = self._ci_input_path('small_sim_reads.fq.gz')
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')
        
        self._run(['toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--graphs', self.test_vg_graph, '--chroms', 'x',
                   '--gcsa_index_cores', '8',
                   '--realTimeLogging', '--logInfo', '--index_name', 'small', '--all_index'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'map', self.jobStoreLocal, 'sample',
                   os.path.join(self.local_outstore, 'small.xg'),
                   os.path.join(self.local_outstore, 'small.gcsa'),
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--fastq', self.sample_reads,
                   '--alignment_cores', '8', '--reads_per_chunk', '8000',
                   '--realTimeLogging', '--logInfo'])
        self._run(['toil', 'clean', self.jobStoreLocal])
        
        self._run(['toil-vg', 'call', self.jobStoreLocal,
                   os.path.join(self.local_outstore, 'small.xg'), 'sample',
                   self.local_outstore, 
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gams', os.path.join(self.local_outstore, 'sample_default.gam'), 
                   '--chroms', 'x', '--call_chunk_size', '20000', '--calling_cores', '4',
                   '--realTimeLogging', '--logInfo'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'vcfeval', self.jobStoreLocal,
                   '--container', self.containerType,
                   '--call_vcf', os.path.join(self.local_outstore, 'sample.vcf.gz'),
                   '--vcfeval_baseline', self.baseline,
                   '--vcfeval_fasta', self.chrom_fa, self.local_outstore,
                   '--clean', 'never',
                   '--vcfeval_opts', ' --squash-ploidy', '--vcfeval_sample', 'sample',
                   '--realTimeLogging', '--logInfo'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertOutput(None, self.local_outstore, f1_threshold=0.95)

    def test_03_sim_small_mapeval_plots(self):
        ''' 
        Test running mapeval directly on the simulated reads and that plotting
        produces output
        '''
        self.test_vg_graph = self._ci_input_path('small.vg')
        self._download_input('NA12877.brca1.bam_1.fq.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')

        # check running mapeval on the vg graph

        self._run(['toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--graphs', self.test_vg_graph, '--chroms', 'x',
                   '--gcsa_index_cores', '8',
                   '--realTimeLogging', '--logInfo', '--index_name', 'small', '--xg_index'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'sim', self.jobStoreLocal,
                   os.path.join(self.local_outstore, 'small.xg'), '2000',
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam', '--sim_chunks', '5', '--maxCores', '8',
                   '--sim_opts', ' -l 150 -p 500 -v 50', '--seed', '1',
                   '--fastq', os.path.join(self.workdir, 'NA12877.brca1.bam_1.fq.gz')])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'mapeval', self.jobStoreLocal,
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--truth', os.path.join(self.local_outstore, 'true.pos'),
                   '--vg-graphs', self.test_vg_graph,
                   '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                   '--gam-names', 'vg', '--realTimeLogging', '--logInfo',
                   '--alignment_cores', '8', '--single-only', '--multipath-only',                 
                   '--maxCores', '8', '--fasta', self.chrom_fa])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertMapEvalOutput(self.local_outstore, 4000, ['vg-mp'], 0.9)

        # check running plot on the mapeval output
        os.unlink(os.path.join(self.local_outstore, 'plots/plot-pr.svg'))
        os.unlink(os.path.join(self.local_outstore, 'plots/plot-qq.svg'))
        os.unlink(os.path.join(self.local_outstore, 'plots/plot-roc.svg'))
        self._run(['toil-vg', 'plot', self.jobStoreLocal,
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--position-stats', os.path.join(self.local_outstore, 'position.results.tsv'),
                   '--realTimeLogging', '--logInfo',
                   '--maxCores', '8'])
        self._run(['toil', 'clean', self.jobStoreLocal])
        self.assertGreater(os.path.getsize(os.path.join(self.local_outstore, 'plots/plot-pr.svg')), 0)
        self.assertGreater(os.path.getsize(os.path.join(self.local_outstore, 'plots/plot-qq.svg')), 0)
        self.assertGreater(os.path.getsize(os.path.join(self.local_outstore, 'plots/plot-roc.svg')), 0)
        
    def test_04_sim_small_mapeval(self):
        ''' 
        Test running mapeval on some gams
        '''
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.chrom_fa = self._ci_input_path('small.fa.gz')
        self._download_input('NA12877.brca1.bam_1.fq.gz')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.bed_regions = self._ci_input_path('small_regions.bed')

        self._run(['toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--graphs', self.test_vg_graph, '--chroms', 'x',
                   '--gcsa_index_cores', '8',
                   '--realTimeLogging', '--logInfo', '--index_name', 'small', '--all_index'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'sim', self.jobStoreLocal,
                   os.path.join(self.local_outstore, 'small.xg'), '2000',
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam', '--sim_chunks', '5', '--maxCores', '8',
                   '--sim_opts', ' -l 150 -p 500 -v 50 -e 0.05 -i 0.01', '--seed', '1'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'map', self.jobStoreLocal, 'sample',
                   os.path.join(self.local_outstore, 'small.xg'),
                   os.path.join(self.local_outstore, 'small.gcsa'),
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                   '--alignment_cores', '3', '--reads_per_chunk', '1000',
                   '--realTimeLogging', '--logInfo', '--interleaved'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        # check running mapeval on the gams

        self._run(['toil-vg', 'mapeval', self.jobStoreLocal,
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--truth', os.path.join(self.local_outstore, 'true.pos'),
                   '--index-bases', os.path.join(self.local_outstore, 'small'),
                   '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                   '--gams', os.path.join(self.local_outstore, 'sample_default.gam'),
                   '--gam-names', 'vg-pe', '--realTimeLogging', '--logInfo',
                   '--maxCores', '8', '--bwa', '--paired-only', '--fasta', self.chrom_fa])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertMapEvalOutput(self.local_outstore, 4000, ['vg-pe', 'bwa-mem-pe'], 0.9)


    def test_05_sim_small_calleval(self):
        ''' 
        Test running calleval on some mapeval ouput
        '''
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.chrom_fa = self._ci_input_path('small.fa.gz')
        self.chrom_fa_nz = self._ci_input_path('small.fa')
        self._download_input('NA12877.brca1.bam_1.fq.gz')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.bed_regions = self._ci_input_path('small_regions.bed')

        self._run(['toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--graphs', self.test_vg_graph, '--chroms', 'x',
                   '--gcsa_index_cores', '8',
                   '--realTimeLogging', '--logInfo', '--index_name', 'small', '--all_index'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'sim', self.jobStoreLocal,
                   os.path.join(self.local_outstore, 'small.xg'), '2000',
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam', '--sim_chunks', '5', '--maxCores', '8',
                   '--sim_opts', ' -l 150 -p 500 -v 50 -e 0.05 -i 0.01', '--seed', '1'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        # check running mapeval on the indexes

        self._run(['toil-vg', 'mapeval', self.jobStoreLocal,
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam-input-xg', os.path.join(self.local_outstore, 'small.xg'),
                   '--index-bases', os.path.join(self.local_outstore, 'small'),
                   '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                   '--gam-names', 'vg', '--realTimeLogging', '--logInfo',
                   '--alignment_cores', '8',
                   '--maxCores', '8', '--bwa', '--fasta', self.chrom_fa])
        self._run(['toil', 'clean', self.jobStoreLocal])
        
        self._assertMapEvalOutput(self.local_outstore, 4000, ['vg', 'vg-pe', 'bwa-mem', 'bwa-mem-pe'], 0.8)
        
        # check running calleval on the mapeval output
        self._run(['toil-vg', 'calleval', self.jobStoreLocal,
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--chroms', 'x',
                   '--xg_paths', os.path.join(self.local_outstore, 'small.xg'),
                   os.path.join(self.local_outstore, 'small.xg'),
                   '--gams', os.path.join(self.local_outstore, 'aligned-vg_default.gam'),
                   os.path.join(self.local_outstore, 'aligned-vg-pe_default.gam'),
                   '--gam_names', 'vg', 'vg-pe',
                   '--realTimeLogging', '--logInfo',
                   '--vcfeval_fasta', self.chrom_fa_nz,
                   '--vcfeval_baseline', self.baseline,
                   '--vcfeval_bed_regions', self.bed_regions,
                   '--sample_name', '1',
                   '--calling_cores', '2',
                   '--call',
                   '--freebayes', '--force_outstore',
                   '--bams', os.path.join(self.local_outstore, 'bwa-mem.bam'),
                   os.path.join(self.local_outstore, 'bwa-mem-pe.bam'),
                   '--bam_names', 'bwa-mem', 'bwa-mem-pe',
                   '--happy', '--surject'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertCallEvalOutput(self.local_outstore, ['vg-call', 'vg-pe-call', 'bwa-mem-fb', 'bwa-mem-pe-fb',
                                                         'vg-pe-surject-fb', 'vg-surject-fb'], 0.02, 0.02)
        
    def test_06_BRCA1_NA12877(self):
        ''' Test sample BRCA1 output, graph construction and use, and local file processing
        '''
        self._download_input('NA12877.brca1.bam_1.fq.gz')
        self._download_input('NA12877.brca1.bam_2.fq.gz')
        self._download_input('snp1kg-brca1.vg')
        self._download_input('platinum_NA12877_BRCA1.vcf.gz.tbi')
        self._download_input('platinum_NA12877_BRCA1.vcf.gz')
        self._download_input('BRCA1.fa.gz')
        
        self.sample_reads = os.path.join(self.workdir, 'NA12877.brca1.bam_1.fq.gz')
        self.sample_reads2 = os.path.join(self.workdir, 'NA12877.brca1.bam_2.fq.gz')
        self.test_vg_graph = os.path.join(self.workdir, 'snp1kg-brca1.vg')
        self.baseline = os.path.join(self.workdir, 'platinum_NA12877_BRCA1.vcf.gz')
        self.chrom_fa = os.path.join(self.workdir, 'BRCA1.fa.gz')
        self.bam_reads = self._ci_input_path('NA12877.brca1.bam')

        self._run(self.base_command +
                  [self.jobStoreLocal, 'NA12877',
                   self.local_outstore,
                   '--clean', 'never',
                   '--fastq', self.sample_reads, self.sample_reads2, '--graphs',
                   self.test_vg_graph, '--chroms', '17', '--index_name', 'index',
                   '--vcf_offsets', '43044293',
                   '--vcfeval_baseline', self.baseline, '--vcfeval_fasta', self.chrom_fa])
        self._run(['toil', 'clean', self.jobStoreLocal])
        
        self._assertOutput('NA12877', self.local_outstore, f1_threshold=0.45)

        # repeat but with bam reads (and not recreating gcsa)
        self._run(self.base_command +
                  [self.jobStoreLocal, 'NA12877',
                   self.local_outstore,
                   '--clean', 'never',
                   '--bam_input_reads', self.bam_reads,  '--graphs',
                   self.test_vg_graph, '--chroms', '17',
                   '--reads_per_chunk', '10000',
                   '--gcsa_index', os.path.join(self.local_outstore, 'index.gcsa'),
                   # single_reads_chunk currently required for bam in jenkins test but can't figure out
                   # why, as can't reproduce problems on the command line
                   '--single_reads_chunk',
                   '--vcf_offsets', '43044293', 
                   '--vcfeval_baseline', self.baseline, '--vcfeval_fasta', self.chrom_fa])
        self._run(['toil', 'clean', self.jobStoreLocal])
        
        self._assertOutput('NA12877', self.local_outstore, f1_threshold=0.45)        

    def test_07_BRCA1_BRCA2_NA12877(self):
        '''  Test pipeline on chase with two chromosomes, in this case both BRCA regions
        '''
        self._download_input('NA12877.brca1.brca2.bam.fq.gz')
        self._download_input('snp1kg-brca1.vg')
        self._download_input('snp1kg-brca2.vg')        
        self._download_input('platinum_NA12877_BRCA1_BRCA2.vcf.gz.tbi')
        self._download_input('platinum_NA12877_BRCA1_BRCA2.vcf.gz')
        self._download_input('BRCA1_BRCA2.fa.gz')
        
        self.sample_reads = os.path.join(self.workdir, 'NA12877.brca1.brca2.bam.fq.gz')
        self.test_vg_graph = os.path.join(self.workdir, 'snp1kg-brca1.vg')
        self.test_vg_graph2 = os.path.join(self.workdir, 'snp1kg-brca2.vg')        
        self.baseline = os.path.join(self.workdir, 'platinum_NA12877_BRCA1_BRCA2.vcf.gz')
        self.chrom_fa = os.path.join(self.workdir, 'BRCA1_BRCA2.fa.gz')
        
        self._run(self.base_command +
                  [self.jobStoreLocal, 'NA12877',
                   self.local_outstore,
                   '--clean', 'never',
                   '--fastq', self.sample_reads, '--graphs',
                   self.test_vg_graph, self.test_vg_graph2, '--chroms', '17', '13',
                   '--vcf_offsets', '43044293', '32314860', '--interleaved',
                   '--single_reads_chunk', '--index_name', 'genome', '--realTimeStderr',
                   '--vcfeval_baseline', self.baseline, '--vcfeval_fasta', self.chrom_fa])
        self._run(['toil', 'clean', self.jobStoreLocal])
        
        self._assertOutput('NA12877', self.local_outstore, f1_threshold=0.70)

        ''' Test running vg call on one gam that contains multiple paths
        '''

        self.sample_gam = os.path.join(self.local_outstore, 'NA12877_both.gam')
        with open(os.path.join(self.local_outstore, 'NA12877_13.gam')) as gam1, \
             open(os.path.join(self.local_outstore, 'NA12877_17.gam')) as gam2, \
             open(self.sample_gam, 'w') as merged_gam:
            shutil.copyfileobj(gam1, merged_gam)
            shutil.copyfileobj(gam2, merged_gam)
             
        self.xg_index = os.path.join(self.local_outstore, 'genome.xg')        

        outstore = self.local_outstore + '.2'
        self._run(['toil-vg', 'call', self.jobStoreLocal,
                   '--container', self.containerType,
                   '--clean', 'never',
                   self.xg_index, 'NA12877', outstore, '--gams', self.sample_gam,
                   '--chroms', '17', '13', '--vcf_offsets', '43044293', '32314860',
                   '--call_chunk_size', '23000', '--calling_cores', '4',
                   '--realTimeLogging', '--realTimeStderr', '--logInfo', '--call_opts', '-E 0'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'vcfeval', self.jobStoreLocal, '--clean', 'never',
                   '--call_vcf', os.path.join(outstore, 'NA12877.vcf.gz'),
                   '--vcfeval_baseline', self.baseline,
                   '--vcfeval_fasta', self.chrom_fa, outstore,
                   '--realTimeLogging', '--realTimeStderr', '--logInfo',
                   '--vcfeval_opts', ' --ref-overlap'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertOutput(None, outstore, f1_threshold=0.70)

        ''' Test surject
        '''
        self._run(['toil-vg', 'surject', self.jobStoreLocal,
                   '--container', self.containerType,
                   '--clean', 'never', outstore,
                   '--reads_per_chunk', '20000',
                   '--gam_input_reads', self.sample_gam, '--paths', '13', '17',
                   '--xg_index', self.xg_index, '--alignment_cores', '2'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        ''' Test recall
        '''
        self._run(['toil-vg', 'call', self.jobStoreLocal,
                   '--container', self.containerType,
                   '--clean', 'never',
                   self.xg_index, 'NA12877', outstore, '--gams', self.sample_gam,
                   '--chroms', '17', '13', '--vcf_offsets', '43044293', '32314860',
                   '--call_chunk_size', '23000', '--calling_cores', '4',
                   '--realTimeLogging', '--realTimeStderr', '--logInfo', '--call_opts', '-E 0', '--recall'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertXREF('NA12877', outstore)        

        # check bam not empty
        self.assertGreater(os.path.getsize(os.path.join(outstore, 'surject.bam')), 250000)

                
    def test_08_sim_small_outstore(self):
        ''' 
        This is the same as test #1, but exercises --force_outstore.
        '''
        self.sample_reads = self._ci_input_path('small_sim_reads.fq.gz')
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')

        self._run(self.base_command +
                  [self.jobStoreLocal, '1',
                   self.local_outstore, '--clean', 'never',  '--fastq', self.sample_reads,
                   '--graphs', self.test_vg_graph,
                   '--chroms', 'x', '--vcfeval_baseline', self.baseline,
                   '--vcfeval_fasta', self.chrom_fa, '--vcfeval_opts', ' --squash-ploidy',
                   '--force_outstore'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertOutput('1', self.local_outstore, f1_threshold=0.95)

    def test_09_construct(self):
        '''
        Test that the output of toil-vg construct is somewhat reasonable
        '''

        in_vcf = self._ci_input_path('1kg_hg38-BRCA1.vcf.gz')
        in_tbi = in_vcf + '.tbi'
        in_fa = self._ci_input_path('17.fa')
        in_region = '17:43044294-43125482'
        out_name = 'snp1kg-BRCA1'
        
        self._run(['toil-vg', 'construct', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--fasta', in_fa, '--vcf', in_vcf, '--regions', in_region,
                   '--out_name', out_name, '--pangenome', '--pos_control', 'HG00096',
                   '--neg_control', 'HG00096', '--sample_graph', 'HG00096',
                   '--filter_ceph', '--realTimeLogging', '--logInfo', '--validate',
                   '--haplo_sample', 'HG00096', '--min_af', '0.6', '--keep_vcfs'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        # This is a fairly superficial check without adding machinery to read vg files
        # Make sure output files exist and that they have expected relative sizes.
        # Todo: better correctness checks (maybe compare to hand-generated data?
        prev_vg_size = None
        prev_vcf_size = None
        for ext in ['', '_filter', '_minus_HG00096', '_HG00096_sample', '_HG00096_haplo', '_minaf_0.6']:
            if ext and ext not in ['_HG00096_haplo', '_minaf_0.6', '_HG00096_sample']:
                vcf_file = os.path.join(self.local_outstore, '{}-vcfs'.format(out_name), '1kg_hg38-BRCA1{}.vcf.gz'.format(ext))

                assert os.path.isfile(vcf_file)
                assert os.path.isfile(vcf_file + '.tbi')

                # use the number of lines in vcf instead of size, since filtering cuts out a bunch
                # of samples
                with gzip.open(vcf_file) as vf:
                    vcf_size = len([line for line in vf])
                if prev_vcf_size is not None:
                    assert vcf_size < prev_vcf_size
                prev_vcf_size = vcf_size

            vg_file = os.path.join(self.local_outstore, '{}{}.vg'.format(out_name, ext))

            assert os.path.isfile(vg_file)

            vg_size = os.path.getsize(vg_file)
            if prev_vg_size is not None:
                assert vg_size < prev_vg_size
            prev_vg_size = vg_size

    def test_10_sim_small_genotype(self):
        ''' 
        This is the same as test #1, but exercises --force_outstore and --genotype
        '''
        self.sample_reads = self._ci_input_path('small_sim_reads.fq.gz')
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')

        self._run(self.base_command +
                  [self.jobStoreLocal, '1',
                   self.local_outstore, '--clean', 'never',
                   '--fastq', self.sample_reads,
                   '--graphs', self.test_vg_graph,
                   '--chroms', 'x', '--vcfeval_baseline', self.baseline,
                   '--vcfeval_fasta', self.chrom_fa, '--vcfeval_opts', ' --squash-ploidy',
                   '--genotype', '--genotype_opts', ' -Q -A'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertOutput('1', self.local_outstore, f1_threshold=0.95)

    def test_11_gbwt(self):
        '''
        Test that the gbwt gets constructed without crashing (but not much beyond that)
        '''

        in_vcf = self._ci_input_path('1kg_hg38-BRCA1.vcf.gz')
        in_tbi = in_vcf + '.tbi'
        in_fa = self._ci_input_path('17.fa')
        in_region = '17:43044294-43125482'

        # make a snp1kg graph with alt paths
        self._run(['toil-vg', 'construct', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--fasta', in_fa, '--vcf', in_vcf, '--regions', in_region,
                   '--out_name', 'snp1kg-BRCA1', '--alt_paths', '--pangenome',
                   '--realTimeLogging', '--logInfo', '--realTimeStderr'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        # check graph exists
        vg_path = os.path.join(self.local_outstore, 'snp1kg-BRCA1.vg')
        self.assertTrue(os.path.isfile(vg_path))

        # make a gbwt and xg index
        self._run(['toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--xg_index', '--graphs', vg_path, '--chroms', '17',
                   '--vcf_phasing', in_vcf, '--index_name', 'my_index',
                   '--gbwt_index', '--xg_index_cores', '4',
                   '--realTimeLogging', '--logInfo', '--realTimeStderr'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        # check gbwt exists
        gbwt_path = os.path.join(self.local_outstore, 'my_index.gbwt')
        self.assertTrue(os.path.isfile(gbwt_path))

        # check gbwt not empty
        self.assertGreater(os.path.getsize(gbwt_path), 250000)
        
    def test_12_sim_small_mapeval_minimap2(self):
        ''' 
        Test minimap2 support in mapeval 
        '''
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.chrom_fa = self._ci_input_path('small.fa.gz')
        self._download_input('NA12877.brca1.bam_1.fq.gz')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.bed_regions = self._ci_input_path('small_regions.bed')
        
        # Index the graphs
        self._run(['toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--graphs', self.test_vg_graph, '--chroms', 'x',
                   '--gcsa_index_cores', '8',
                   '--realTimeLogging', '--logInfo', '--index_name', 'small', '--all_index'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        # Simulate the reads
        self._run(['toil-vg', 'sim', self.jobStoreLocal,
                   os.path.join(self.local_outstore, 'small.xg'), '2000',
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam', '--sim_chunks', '5', '--maxCores', '8',
                   '--sim_opts', ' -l 150 -p 500 -v 50 -e 0.05 -i 0.01', '--seed', '1'])
        self._run(['toil', 'clean', self.jobStoreLocal])
                   
        # Run mapeval with minimap2
        self._run(['toil-vg', 'mapeval', self.jobStoreLocal,
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam-input-xg', os.path.join(self.local_outstore, 'small.xg'),
                   '--index-bases', os.path.join(self.local_outstore, 'small'),
                   '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                   '--gam-names', 'vg', '--realTimeLogging', '--logInfo',
                   '--alignment_cores', '8', '--validate',
                   '--maxCores', '8', '--minimap2', '--fasta', self.chrom_fa])
        self._run(['toil', 'clean', self.jobStoreLocal])
        
        # TODO: Minimap2 is quite inaccurate on this tiny test. Maybe it only works well at larger scales?
        # Note that mpmap2 only runs on paired end reads.
        self._assertMapEvalOutput(self.local_outstore, 4000, ['vg', 'vg-pe', 'minimap2-pe'], 0.6)
        
    def test_13_sim_small_mapeval_snarls(self):
        ''' 
        Test running mapeval with and without snarls
        '''
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.chrom_fa = self._ci_input_path('small.fa.gz')
        self._download_input('NA12877.brca1.bam_1.fq.gz')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.bed_regions = self._ci_input_path('small_regions.bed')
        
        # Index the graphs
        self._run(['toil-vg', 'index', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--graphs', self.test_vg_graph, '--chroms', 'x',
                   '--gcsa_index_cores', '8',
                   '--realTimeLogging', '--logInfo', '--index_name', 'small', '--all_index'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        # Simulate the reads
        self._run(['toil-vg', 'sim', self.jobStoreLocal,
                   os.path.join(self.local_outstore, 'small.xg'), '2000',
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam', '--sim_chunks', '5', '--maxCores', '8',
                   '--sim_opts', ' -l 150 -p 500 -v 50 -e 0.05 -i 0.01', '--seed', '1', '--validate'])
        self._run(['toil', 'clean', self.jobStoreLocal])
                   
        # Run mapeval with minimap2
        self._run(['toil-vg', 'mapeval', self.jobStoreLocal,
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam-input-xg', os.path.join(self.local_outstore, 'small.xg'),
                   '--index-bases', os.path.join(self.local_outstore, 'small'),
                   '--gam_input_reads', os.path.join(self.local_outstore, 'sim.gam'),
                   '--gam-names', 'vg', 
                   '--use-snarls', '--strip-snarls', '--multipath', '--paired-only',
                   '--realTimeLogging', '--logInfo',
                   '--alignment_cores', '8',
                   '--maxCores', '8'])
        self._run(['toil', 'clean', self.jobStoreLocal])
        
        # Note that snarls only matter for mpmap
        self._assertMapEvalOutput(self.local_outstore, 4000, ['vg-pe', 'vg-mp-pe', 'vg-nosnarls-mp-pe'], 0.8)

    def test_14_construct_naming_options(self):
        """
        Make sure we don't get crashes when mixing and matching chromosomes with and without chr
        prefix when using the appropriate options
        """
        in_vcf = self._ci_input_path('1kg_hg38-BRCA1.vcf.gz')
        in_tbi = in_vcf + '.tbi'
        self._download_input('17.fa')
        fa_path = os.path.join(self.workdir, '17.fa')
        subprocess.check_call(['sed', '-i', 's/17/chr17/g', fa_path])        
        in_region = '17:43044294-43125482'
        
        self._run(['toil-vg', 'construct', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--fasta', fa_path, '--vcf', in_vcf, '--regions', in_region,
                   '--out_name', 'snp1kg-BRCA1', '--pangenome', '--pos_control', 'HG00096',
                   '--realTimeLogging', '--logInfo', '--validate', '--add_chr_prefix', '--mask_ambiguous'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'construct', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--fasta', fa_path, '--vcf', in_vcf, '--regions', in_region, '--handle_svs',
                   '--out_name', 'snp1kg-BRCA1', '--pangenome', '--pos_control', 'HG00096',
                   '--realTimeLogging', '--logInfo', '--validate', '--remove_chr_prefix'])
        self._run(['toil', 'clean', self.jobStoreLocal])


    def test_15_sim_small_no_call_chunking(self):
        ''' 
        This is the same as test #1, but exercises --call_chunk_size 0
        '''
        self.sample_reads = self._ci_input_path('small_sim_reads.fq.gz')
        self.test_vg_graph = self._ci_input_path('small.vg')
        self.baseline = self._ci_input_path('small.vcf.gz')
        self.chrom_fa = self._ci_input_path('small.fa.gz')

        self.base_command[self.base_command.index('--call_chunk_size') + 1] = '0'

        self._run(self.base_command +
                  [self.jobStoreLocal, '1',
                   self.local_outstore, '--clean', 'never',
                   '--fastq', self.sample_reads,
                   '--graphs', self.test_vg_graph,
                   '--chroms', 'x', '--vcfeval_baseline', self.baseline,
                   '--vcfeval_fasta', self.chrom_fa, '--vcfeval_opts', ' --squash-ploidy'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._assertOutput('1', self.local_outstore, f1_threshold=0.95)

    def test_16_sv_genotyping(self):
        '''
        End to end SV genotyping on chr21 and chr22 of the HGSVC graph.  
        We subset reads and variants to chr21:5000000-6000000 and chr22:10000000-11000000
        and the fastas are subset to chr21:1-7000000 and chr22:1-12000000
        '''

        fa_path = self._ci_input_path('hg38_chr21_22.fa.gz')
        vcf_path = self._ci_input_path('HGSVC_regions.vcf.gz')
        gam_reads_path = self._ci_input_path('HGSVC_regions.gam')

        self._run(['toil-vg', 'construct', self.jobStoreLocal, self.local_outstore,
                   '--container', self.containerType,
                   '--gcsa_index_cores', '8',
                   '--clean', 'never',
                   '--fasta', fa_path,
                   '--regions', 'chr21', 'chr22',
                   '--vcf', vcf_path,
                   '--out_name', 'HGSVC', '--pangenome', '--flat_alts', '--alt_path_gam_index', '--xg_index', '--gcsa_index'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'map', self.jobStoreLocal, 'HG00514',
                   os.path.join(self.local_outstore, 'HGSVC.xg'),
                   os.path.join(self.local_outstore, 'HGSVC.gcsa'),
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--gam_input_reads', gam_reads_path,
                   '--interleaved',
                   '--alignment_cores', '8', 
                   '--single_reads_chunk',
                   '--realTimeLogging', '--logInfo'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        self._run(['toil-vg', 'call', self.jobStoreLocal,
                   os.path.join(self.local_outstore, 'HGSVC.xg'),
                   'HG00514',
                   self.local_outstore,
                   '--container', self.containerType,
                   '--clean', 'never',
                   '--chroms', 'chr21', 'chr22',
                   '--call_chunk_cores', '8',
                   '--recall_context', '200',
                   '--gams', os.path.join(self.local_outstore, 'HG00514_default.gam'),
                   '--alt_path_gam', os.path.join(self.local_outstore, 'HGSVC_alts.gam'),
                   '--genotype_vcf', vcf_path,
                   '--call_chunk_cores', '8'])
        self._run(['toil', 'clean', self.jobStoreLocal])

        
        self._run(['toil-vg', 'vcfeval', self.jobStoreLocal,
                   self.local_outstore,
                   '--sveval',
                   '--vcfeval_baseline', vcf_path,
                   '--vcfeval_sample', 'HG00514',
                   '--normalize',
                   '--vcfeval_fasta', fa_path,
                   '--call_vcf', os.path.join(self.local_outstore, 'HG00514.vcf.gz')])
        self._run(['toil', 'clean', self.jobStoreLocal])
                   
        self._assertSVEvalOutput(self.local_outstore, f1_threshold=0.42)           
        
    def _run(self, args):
        log.info('Running %r', args)
        subprocess.check_call(args)

    def _assertOutput(self, sample_name, outstore, f1_threshold=0.90):

        # grab the f1.txt file
        local_f1 = os.path.join(self.workdir, 'f1.txt')
        io_store = IOStore.get(outstore)
        f1_path = 'vcfeval_output_f1.txt'
        if sample_name:
            f1_path = sample_name + '_' + f1_path
        io_store.read_input_file(f1_path, local_f1)

        with open(local_f1) as f1_file:
            f1_score = float(f1_file.readline().strip())
        print f1_score
        self.assertGreaterEqual(f1_score, f1_threshold)

    def _assertMapEvalOutput(self, outstore, test_count, names, acc_threshold):
        with open(os.path.join(outstore, 'stats.tsv')) as stats:
            table = [line for line in stats]
        headers = set()
        for row in table[1:]:
            toks = row.split()
            if len(toks) != 6:
                raise RuntimeError("toks should have 6 entries but is {}".format(toks))
            self.assertEqual(len(toks), 6)
            name, count, acc, auc, qqr, maxf1 = \
                toks[0], int(toks[1]), float(toks[2]), float(toks[3]), float(toks[4]), float(toks[5])
            headers.add(name)
            self.assertEqual(count, test_count)
            self.assertGreater(acc, acc_threshold)
            # todo: look into this more. 
            #self.assertTrue(auc > 0.9)
            #self.assertTrue(qqur > -10)
        self.assertEqual(headers, set(names))
        # make sure plots get drawn
        self.assertGreater(os.path.getsize(os.path.join(outstore, 'plots/plot-pr.svg')), 0)
        self.assertGreater(os.path.getsize(os.path.join(outstore, 'plots/plot-qq.svg')), 0)

    def _assertCallEvalOutput(self, outstore, names, f1_threshold, happy_snp_f1_threshold=-1,
                              happy_indel_f1_threshold=-1):
        with open(os.path.join(outstore, 'calleval_stats.tsv')) as stats:
            table = [line for line in stats]
        headers = set()
        for row in table:
            toks = row.split()
            self.assertEqual(len(toks), 4)
            name, f1, happy_snp_f1, happy_indel_f1, = toks[0], toks[1], toks[2], toks[3]
            headers.add(name)
            self.assertGreaterEqual(float(f1), f1_threshold)
            self.assertGreaterEqual(float(happy_snp_f1), happy_snp_f1_threshold)
            self.assertGreaterEqual(float(happy_indel_f1), happy_indel_f1_threshold)
        self.assertEqual(headers, set(names))

    def _assertXREF(self, sample, outstore):
        var_count = 0
        xref_count = 0
        with gzip.open(os.path.join(outstore, '{}.vcf.gz'.format(sample)), 'rb') as vcf_file:
            for line in vcf_file:
                if line.strip() and line.strip()[0] != '#':
                    # TODO: clean up call (and/or recall options) to get rid of this hack
                    if len(line.split()) > 4 and line.split()[4] != '.':
                        var_count += 1
                        if 'XREF' in line:
                            xref_count += 1
        self.assertEqual(var_count, xref_count)

    def _assertSVEvalOutput(self, outstore, f1_threshold):
        with open(os.path.join(outstore, 'sv_accuracy.tsv')) as sv_stats:
            table = [line.split() for line in sv_stats]
        self.assertEqual(table[0][-1], 'F1')
        self.assertGreater(float(table[1][-1]), f1_threshold)
                
    def tearDown(self):
        if not self.saveWorkDir:
            shutil.rmtree(self.workdir)
        subprocess.check_call(['toil', 'clean', self.jobStoreLocal])
        
