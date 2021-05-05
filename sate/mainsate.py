#! /usr/bin/env python

"""Main script of SATe in command-line mode
"""

# This file is part of SATe

# SATe is free software: you can redistribute it and/or modify
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
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Jiaye Yu and Mark Holder, University of Kansas


import os
import re
import sys
import signal
import time
import glob
import optparse
import sate

from sate import PROGRAM_NAME, PROGRAM_VERSION, PROGRAM_LONG_DESCRIPTION, get_logger, set_timing_log_filepath, TIMING_LOG, MESSENGER
from alignment import Alignment, SequenceDataset, MultiLocusDataset
from configure import get_configuration, get_input_source_directory
from tools import *
from satejob import *
from treeholder import read_and_encode_splits
from scheduler import start_worker, jobq
from utility import IndentedHelpFormatterWithNL
from filemgr import open_with_intermediates
from satejob import SateTeam
from sate import filemgr
from sate import TEMP_SEQ_ALIGNMENT_TAG, TEMP_TREE_TAG
from util.transformscore import TransformScore

_RunningJobs = None

_LOG = get_logger(__name__)


def fasttree_to_raxml_model_str(datatype, model_str):
    dtu = datatype.upper()
    msu = model_str.upper()
    if dtu == "PROTEIN":
        if "-WAG" in msu:
            if "-GAMMA" in msu:
                return "PROTGAMMAWAGF"
            return "PROTCATWAGF"
        if "-GAMMA" in msu:
            return "PROTGAMMAJTTF"
        return "PROTCATJTTF"
    if "-GAMMA" in msu:
        return "GTRGAMMA"
    return "GTRCAT"
    

def get_auto_defaults_from_summary_stats(datatype, ntax_nchar_tuple_list, total_num_tax):
    """
    Returns nested dictionaries with the following keys set:

    "commandline" : ["multilocus", "datatype"],
    "sate" : ["tree_estimator",  "aligner", "merger", "break_strategy",
              "move_to_blind_on_worse_score",  "start_tree_search_from_current",
              "after_blind_iter_without_imp_limit", "max_subproblem_size", 
              "max_subproblem_frac", "num_cpus", 
              "time_limit", "after_blind_time_without_imp_limit"],
    "fasttree" : ["model", "GUI_model']


    DO NOT delete keys from this dictionary without making sure that the GUI
        code can deal with your change! This code is used to keep the --auto
        command line option and the data-set dependent defaults of the GUI 
        in sync!
    """
    new_defaults = {}
    new_sate_defaults = {
        'tree_estimator' : 'fasttree',
        'aligner' : 'mafft',
        'merger' : 'muscle',
        'break_strategy' : 'centroid',
        'move_to_blind_on_worse_score' : True,
        'start_tree_search_from_current' : True,
        'after_blind_iter_without_imp_limit' : 1,
        'time_limit' : -1,
        'after_blind_time_without_imp_limit' : -1
        }
    if total_num_tax > 400:
        new_sate_defaults['max_subproblem_size'] = 200
        new_sate_defaults['max_subproblem_frac'] = 200.0/total_num_tax
    else:
        new_sate_defaults['max_subproblem_size'] = int(math.ceil(total_num_tax/2.0))
        new_sate_defaults['max_subproblem_frac'] = 0.5
    if datatype.lower() == 'protein':
        new_defaults['fasttree'] = {
            'model' : '-wag -gamma',
            'GUI_model' : 'WAG+G20'
            }
    else:
        new_defaults['fasttree'] = {
            'model' : '-gtr -gamma',
            'GUI_model' : 'GTR+G20'
            }
     
    num_cpu = 1
    try:
        import multiprocessing
        num_cpu = multiprocessing.cpu_count()
    except:
        pass
    new_sate_defaults['num_cpus'] = num_cpu
        
    new_defaults['sate'] = new_sate_defaults
    new_commandline_defaults = {
        'datatype' : datatype.lower()
        }
    new_commandline_defaults['multilocus'] = bool(len(ntax_nchar_tuple_list) > 1)
    new_defaults['commandline'] = new_commandline_defaults
    _LOG.debug('Auto defaults dictionary: %s' % str(new_defaults))
    return new_defaults

def killed_handler(n, frame):
    global _RunningJobs
    if _RunningJobs:
        MESSENGER.send_warning("signal killed_handler called. Killing running jobs...\n")
        if isinstance(_RunningJobs, list):
            for j in _RunningJobs:
                j.kill()
                MESSENGER.send_warning("kill called...\n")
        else:
            j = _RunningJobs
            j.kill()
            MESSENGER.send_warning("kill called...\n")
    else:
        MESSENGER.send_warning("signal killed_handler called with no jobs running. Exiting.\n")
    sys.exit()

def read_input_sequences(seq_filename_list,
        datatype,
        missing=None):
    md = MultiLocusDataset()
    md.read_files(seq_filename_list=seq_filename_list,
            datatype=datatype,
            missing=missing)
    return md

def finish_sate_execution(sate_team,
                          user_config,
                          temporaries_dir,
                          multilocus_dataset,
                          sate_products):
    global _RunningJobs
    # get the RAxML model #TODO: this should check for the tree_estimator.  Currently we only support raxml, so this works...
    model = user_config.raxml.model

    options = user_config.commandline

    user_config.save_to_filepath(os.path.join(temporaries_dir, 'last_used.cfg'))
    if options.timesfile:
        f = open_with_intermediates(options.timesfile, 'a')
        f.close()
        set_timing_log_filepath(options.timesfile)
    ############################################################################
    # We must read the incoming tree in before we call the get_sequences_for_sate
    #   function that relabels that taxa in the dataset
    ######
    alignment_as_tmp_filename_to_report = None
    tree_as_tmp_filename_to_report = None
    
    tree_file = options.treefile
    if tree_file:
        if not os.path.exists(tree_file):
            raise Exception('The tree file "%s" does not exist' % tree_file)
        tree_f = open(tree_file, 'rU')
        MESSENGER.send_info('Reading starting trees from "%s"...' % tree_file)
        try:
            tree_list = read_and_encode_splits(multilocus_dataset.dataset, tree_f,
                    starting_tree=True)
        except KeyError:
            MESSENGER.send_error("Error in reading the treefile, probably due to a name in the tree that does not match the names in the input sequence files.\n")
            raise
        except:
            MESSENGER.send_error("Error in reading the treefile.\n")
            raise
        tree_f.close()
        if len(tree_list) > 1:
            MESSENGER.send_warning('%d starting trees found in "%s". The first tree will be used.' % (len(tree_list), tree_file))
        starting_tree = tree_list[0]
        score = None
        tree_as_tmp_filename_to_report = tree_file

    ############################################################################
    # This will relabel the taxa if they have problematic names
    #####
    multilocus_dataset.relabel_for_sate()

    ############################################################################
    # This ensures all nucleotide data is DNA internally
    #####
    restore_to_rna = False
    if user_config.commandline.datatype.upper() == 'RNA':
        multilocus_dataset.convert_rna_to_dna()
        user_config.commandline.datatype = 'DNA'
        restore_to_rna = True

    export_names = True
    if export_names:
        try:
            name_filename = sate_products.get_abs_path_for_tag('name_translation.txt')
            name_output = open(name_filename, 'w')
            safe2real = multilocus_dataset.safe_to_real_names
            safe_list = safe2real.keys()
            safe_list.sort()
            for safe in safe_list:
                orig = safe2real[safe][0]
                name_output.write("%s\n%s\n\n" % (safe, orig))
            name_output.close()
            MESSENGER.send_info("Name translation information saved to %s as safe name, original name, blank line format." % name_filename)
        except:
            MESSENGER.send_info("Error exporting saving name translation to %s" % name_filename)
            
    
    if options.aligned:
        options.aligned = all( [i.is_aligned() for i in multilocus_dataset] )

    ############################################################################
    # Launch threads to do work
    #####
    sate_config = user_config.get("sate")
    start_worker(sate_config.num_cpus)

    ############################################################################
    # Be prepared to kill any long running jobs
    #####
    prev_signals = []
    for sig in [signal.SIGTERM, signal.SIGABRT, signal.SIGINT]: # signal.SIGABRT, signal.SIGBUS, signal.SIGINT, signal.SIGKILL, signal.SIGSTOP]:
        prev_handler = signal.signal(sig, killed_handler)
        prev_signals.append((sig, prev_handler))

    try:
        if (not options.two_phase) and tree_file:
            # getting the newick string here will allow us to get a string that is in terms of the correct taxon labels
            starting_tree_str = starting_tree.compose_newick()
        else:
            if not options.two_phase:
                MESSENGER.send_info("Creating a starting tree for the SATe algorithm...")
            if (options.two_phase) or (not options.aligned):
                MESSENGER.send_info("Performing initial alignment of the entire data matrix...")
                init_aln_dir = os.path.join(temporaries_dir, 'init_aln')
                init_aln_dir = sate_team.temp_fs.create_subdir(init_aln_dir)
                delete_aln_temps = not (options.keeptemp and options.keepalignmenttemps)
                new_alignment_list= []
                aln_job_list = []
                for unaligned_seqs in multilocus_dataset:
                    job = sate_team.aligner.create_job(unaligned_seqs,
                                                       tmp_dir_par=init_aln_dir,
                                                       context_str="initalign",
                                                       delete_temps=delete_aln_temps)
                    aln_job_list.append(job)
                _RunningJobs = aln_job_list
                for job in aln_job_list:
                    jobq.put(job)
                for job in aln_job_list:
                    new_alignment = job.get_results()
                    new_alignment_list.append(new_alignment)
                _RunningJobs = None
                for locus_index, new_alignment in enumerate(new_alignment_list):
                    multilocus_dataset[locus_index] = new_alignment
                if delete_aln_temps:
                    sate_team.temp_fs.remove_dir(init_aln_dir)
            else:
                MESSENGER.send_info("Input sequences assumed to be aligned (based on sequence lengths).")

            MESSENGER.send_info("Performing initial tree search to get starting tree...")
            init_tree_dir = os.path.join(temporaries_dir, 'init_tree')
            init_tree_dir = sate_team.temp_fs.create_subdir(init_tree_dir)
            delete_tree_temps = not options.keeptemp
            job = sate_team.tree_estimator.create_job(multilocus_dataset,
                                                    tmp_dir_par=init_tree_dir,
                                                    num_cpus=sate_config.num_cpus,
                                                    context_str="inittree",
                                                    delete_temps=delete_tree_temps,
                                                    sate_products=sate_products,
                                                    step_num='initialsearch')
            _RunningJobs = job
            jobq.put(job)
            score, starting_tree_str = job.get_results()
            man_weights = {'w_simg': options.w_simg, 'w_simng': options.w_simng, 'w_sp': options.w_sp, 'w_gap': options.w_gap, 'w_ml': options.w_ml}
            #w1 = options.w_simg
            score = TransformScore(multilocus_dataset, score, man_weights).execute()  #w_simg=options.w_simg, w_simng=options.w_simng, w_sp=options.w_sp, w_gap=options.w_gap, w_ml=options.w_ml).execute() # MAN: need to transform score (ml) to our composite 5 objective score: simg, simng, sp, gap, ml
            _RunningJobs = None
            alignment_as_tmp_filename_to_report = sate_products.get_abs_path_for_iter_output("initialsearch", TEMP_SEQ_ALIGNMENT_TAG, allow_existing=True)
            tree_as_tmp_filename_to_report = sate_products.get_abs_path_for_iter_output("initialsearch", TEMP_TREE_TAG, allow_existing=True)
            if delete_tree_temps:
                sate_team.temp_fs.remove_dir(init_tree_dir)
        _LOG.debug('We have the tree and whole_alignment, partitions...')

        sate_config_dict = sate_config.dict()

        if options.keeptemp:
            sate_config_dict['keep_iteration_temporaries'] = True
            if options.keepalignmenttemps:
                sate_config_dict['keep_realignment_temporaries'] = True
        sate_config_dict['man_weights'] = man_weights
        job = SateJob(multilocus_dataset=multilocus_dataset,
                        sate_team=sate_team,
                        name=options.job,
                        status_messages=MESSENGER.send_info,
                        score=score, # MAN: to init best_score
                        **sate_config_dict)
        job.tree_str = starting_tree_str
        job.curr_iter_align_tmp_filename = alignment_as_tmp_filename_to_report
        job.curr_iter_tree_tmp_filename = tree_as_tmp_filename_to_report
        if score is not None:
            job.store_optimum_results(new_multilocus_dataset=multilocus_dataset,
                    new_tree_str=starting_tree_str,
                    new_score=score,
                    curr_timestamp=time.time())

        if options.two_phase:
            MESSENGER.send_info("Exiting with the initial tree because the SATe algorithm is avoided when the --two-phase option is used.")
        else:
            _RunningJobs = job
            MESSENGER.send_info("Starting SATe algorithm on initial tree...")
            job.run(tmp_dir_par=temporaries_dir, sate_products=sate_products)
            _RunningJobs = None

            if job.return_final_tree_and_alignment:
                alignment_as_tmp_filename_to_report = job.curr_iter_align_tmp_filename
            else:
                alignment_as_tmp_filename_to_report = job.best_alignment_tmp_filename
            
            if user_config.commandline.raxml_search_after:
                raxml_model = user_config.raxml.model.strip()
                if not raxml_model:
                    dt = user_config.commandline.datatype
                    mf = sate_team.tree_estimator.model
                    ms =  fasttree_to_raxml_model_str(dt, mf)
                    sate_team.raxml_tree_estimator.model = ms
                rte = sate_team.raxml_tree_estimator
                MESSENGER.send_info("Performing post-processing tree search in RAxML...")
                post_tree_dir = os.path.join(temporaries_dir, 'post_tree')
                post_tree_dir = sate_team.temp_fs.create_subdir(post_tree_dir)
                delete_tree_temps = not options.keeptemp
                starting_tree = None
                if user_config.sate.start_tree_search_from_current:
                    starting_tree = job.tree
                post_job = rte.create_job(job.multilocus_dataset,
                                    starting_tree=starting_tree,
                                    num_cpus=sate_config.num_cpus,
                                    context_str="postraxtree",
                                    tmp_dir_par=post_tree_dir,
                                    delete_temps=delete_tree_temps,
                                    sate_products=sate_products,
                                    step_num="postraxtree")
                _RunningJobs = post_job
                jobq.put(post_job)
                post_score, post_tree = post_job.get_results()
                _RunningJobs = None
                tree_as_tmp_filename_to_report = sate_products.get_abs_path_for_iter_output("postraxtree", TEMP_TREE_TAG, allow_existing=True)
                if delete_tree_temps:
                    sate_team.temp_fs.remove_dir(post_tree_dir)
                job.tree_str = post_tree
                job.score = post_score
                if post_score > job.best_score:
                    job.best_tree_str = post_tree
                    job.best_score = post_score
            else:
                if job.return_final_tree_and_alignment:
                    tree_as_tmp_filename_to_report = job.curr_iter_tree_tmp_filename
                else:
                    tree_as_tmp_filename_to_report = job.best_tree_tmp_filename


        #######################################################################
        # Restore original taxon names and RNA characters
        #####
        job.multilocus_dataset.restore_taxon_names()
        if restore_to_rna:
            job.multilocus_dataset.convert_dna_to_rna()
            user_config.commandline.datatype = 'RNA'

        assert len(sate_products.alignment_streams) == len(job.multilocus_dataset)
        for i, alignment in enumerate(job.multilocus_dataset):
            alignment_stream = sate_products.alignment_streams[i]
            MESSENGER.send_info("Writing resulting alignment to %s" % alignment_stream.name)
            alignment.write(alignment_stream, file_format="FASTA")
            alignment_stream.close()


        MESSENGER.send_info("Writing resulting tree to %s" % sate_products.tree_stream.name)
        tree_str = job.tree.compose_newick()
        sate_products.tree_stream.write("%s;\n" % tree_str)


        #outtree_fn = options.result
        #if outtree_fn is None:
        #    if options.multilocus:
        #        outtree_fn = os.path.join(seqdir, "combined_%s.tre" % options.job)
        #    else:
        #        outtree_fn = aln_filename + ".tre"
        #MESSENGER.send_info("Writing resulting tree to %s" % outtree_fn)
        #tree_str = job.tree.compose_newick()
        #sate_products.tree_stream.write("%s;\n" % tree_str)


        MESSENGER.send_info("Writing resulting likelihood score to %s" % sate_products.score_stream.name)
        sate_products.score_stream.write("%s\n" % job.score)
        
        if alignment_as_tmp_filename_to_report is not None:
            MESSENGER.send_info('The resulting alignment (with the names in a "safe" form) was first written as the file "%s"' % alignment_as_tmp_filename_to_report)
        if tree_as_tmp_filename_to_report is not None:
            MESSENGER.send_info('The resulting tree (with the names in a "safe" form) was first written as the file "%s"' % tree_as_tmp_filename_to_report)

    finally:
        for el in prev_signals:
            sig, prev_handler = el
            if prev_handler is None:
                signal.signal(sig, signal.SIG_DFL)
            else:
                signal.signal(sig, prev_handler)

def run_sate_from_config(user_config, sate_products):
    """
    Returns (None, None) if no temporary directory is left over from the run
    or returns (dir, temp_fs) where `dir` is the path to the temporary
    directory created for the scratch files and `temp_fs` is the TempFS
    instance used to create `dir`
    """

    multilocus_dataset = read_input_sequences(user_config.input_seq_filepaths,
            datatype=user_config.commandline.datatype,
            missing=user_config.commandline.missing)
    cmdline_options = user_config.commandline

    ############################################################################
    # Create the safe directory for temporaries
    # The general form of the directory is
    #   ${options.temporaries}/${options.job}/temp${RANDOM}
    ######
    par_dir = cmdline_options.temporaries
    if par_dir is None:
        par_dir = os.path.join(os.path.expanduser('~'), '.sate')
    cmdline_options.job = coerce_string_to_nice_outfilename(cmdline_options.job, "Job", "satejob")
    subdir = cmdline_options.job
    par_dir = os.path.abspath(os.path.join(par_dir, subdir))
    if not os.path.exists(par_dir):
        os.makedirs(par_dir) # this parent directory will not be deleted, so we don't store it in the sate_team.temp_fs

    sate_team = SateTeam(config=user_config)

    delete_dir = not cmdline_options.keeptemp

    temporaries_dir = sate_team.temp_fs.create_top_level_temp(parent=par_dir, prefix='temp')
    assert(os.path.exists(temporaries_dir))
    try:
        MESSENGER.send_info("Directory for temporary files created at %s" % temporaries_dir)
        finish_sate_execution(sate_team=sate_team,
                              user_config=user_config,
                              temporaries_dir=temporaries_dir,
                              multilocus_dataset=multilocus_dataset,
                              sate_products=sate_products)
    finally:
        if delete_dir:
            sate_team.temp_fs.remove_dir(temporaries_dir)
    if delete_dir:
        return None, None
    else:
        return temporaries_dir, sate_team.temp_fs

def coerce_string_to_nice_outfilename(p, reason, default):
    illegal_filename_pattern = re.compile(r'[^-_a-zA-Z0-9.]')
    j = "".join(illegal_filename_pattern.split(p))
    if not j:
        j = default
    if j != p:
        MESSENGER.send_warning('%s name changed from "%s" to "%s" (a safer name for filepath)' % (reason, p, j))
    return j

def sate_main(argv=sys.argv):
    '''Returns (True, dir, temp_fs) on successful execution or raises an exception.

    Where `dir` is either None or the undeleted directory of temporary files.
    and `temp_fs` is is the TempFS object used to create `dir` (if `dir` is
    not None)

    Note that if `argv` is sys.argv then the first element will be skipped, but
        if it is not the sys.argv list then the first element will be interpretted
        as an argument (and will NOT be skipped).
    '''

    _START_TIME = time.time()
    usage = """usage: %prog [options] <settings_file1> <settings_file2> ..."""
    parser = optparse.OptionParser(usage=usage,
                                    description=PROGRAM_LONG_DESCRIPTION,
                                    formatter=IndentedHelpFormatterWithNL(),
                                    version="%s v%s" % (PROGRAM_NAME, PROGRAM_VERSION))

    user_config = get_configuration()
    command_line_group = user_config.get('commandline')
    command_line_group.add_to_optparser(parser)
    sate_group = user_config.get('sate')
    sate_group.add_to_optparser(parser)
    
    group = optparse.OptionGroup(parser, "SATe tools extra options")
    group.add_option('--tree-estimator-model', type='string',
            dest='tree_estimator_model',
            help='Do not use this option.')
    group.add_option('--simg', type='float', dest='w_simg', help='weight for SimG')
    group.add_option('--simng', type='float', dest='w_simng', help='weight for SimNG')
    group.add_option('--osp', type='float', dest='w_sp', help='weight for SP')
    group.add_option('--gap', type='float', dest='w_gap', help='weight for Gap')
    group.add_option('--ml', type='float', dest='w_ml', help='weight for ML')
    parser.add_option_group(group)
    
    if argv == sys.argv:
        (options, args) = parser.parse_args(argv[1:])
    else:
        (options, args) = parser.parse_args(argv)
    #if options.multilocus:
    #    sys.exit("SATe: Multilocus mode is disabled in this release.")
    w_simg = sate.usersettingclasses.StringUserSetting('w_simg', -1)
    user_config.commandline.add_option('w_simg', w_simg)
    w_simng = sate.usersettingclasses.StringUserSetting('w_simng', -1)
    user_config.commandline.add_option('w_simng', w_simng)
    w_sp = sate.usersettingclasses.StringUserSetting('w_sp', -1)
    user_config.commandline.add_option('w_sp', w_sp)
    w_gap = sate.usersettingclasses.StringUserSetting('w_gap', -1)
    user_config.commandline.add_option('w_gap', w_gap)
    w_ml = sate.usersettingclasses.StringUserSetting('w_ml', -1)
    user_config.commandline.add_option('w_ml', w_ml)


    if options.tree_estimator_model and options.tree_estimator and len(args) == 0:
        if options.tree_estimator.lower() == 'raxml':
            user_config.raxml.model = options.tree_estimator_model
        elif options.tree_estimator.lower() == 'fasttree':
            user_config.fasttree.model = options.tree_estimator_model

    config_filenames = list(args)
    for fn in config_filenames:
        if fn[0] == '"' and fn[-1] == '"':
            fn = fn[1:-1]
        if not os.path.exists(fn):
            raise Exception('The configuration (settings) file "%s" does not exist' % fn)
        try:
            user_config.read_config_filepath(fn)
        except:
            raise Exception('The file "%s" does not appear to be a valid configuration file format. It lacks section headers.' % fn)
    user_config.set_values_from_dict(options.__dict__)
    command_line_group.job = coerce_string_to_nice_outfilename(command_line_group.job, 'Job', 'satejob')


    if user_config.commandline.auto or (user_config.commandline.untrusted):
        if user_config.commandline.input is None:
            sys.exit("ERROR: Input file(s) not specified.")
        from sate.usersettingclasses import get_list_of_seq_filepaths_from_dir
        from sate.alignment import summary_stats_from_parse
        try:
            if user_config.commandline.multilocus:
                fn_list = get_list_of_seq_filepaths_from_dir(user_config.commandline.input)
            else:
                fn_list = [user_config.commandline.input]
            datatype_list = [user_config.commandline.datatype.upper()]
            careful_parse = user_config.commandline.untrusted
            summary_stats = summary_stats_from_parse(fn_list, datatype_list, careful_parse=careful_parse)
        except:
            if user_config.commandline.auto:
                MESSENGER.send_error("Error reading input while setting options for the --auto mode\n")
            else:
                MESSENGER.send_error("Error reading input\n")
            raise
        if user_config.commandline.auto:
            user_config.commandline.auto = False
            auto_opts = get_auto_defaults_from_summary_stats(summary_stats[0], summary_stats[1], summary_stats[2])
            user_config.get('sate').set_values_from_dict(auto_opts['sate'])
            user_config.get('commandline').set_values_from_dict(auto_opts['commandline'])
            user_config.get('fasttree').set_values_from_dict(auto_opts['fasttree'])
            
    
    if user_config.commandline.raxml_search_after:
        if user_config.sate.tree_estimator.upper() != 'FASTTREE':
            sys.exit("ERROR: the 'raxml_search_after' option is only supported when the tree_estimator is FastTree")

    exportconfig = command_line_group.exportconfig
    if exportconfig:
        command_line_group.exportconfig = None
        user_config.save_to_filepath(exportconfig)

        ### TODO: wrap up in messaging system
        sys.stdout.write('Configuration written to "%s". Exiting successfully.\n' % exportconfig )

        return True, None, None

    if user_config.commandline.input is None:
        sys.exit("ERROR: Input file(s) not specified.")

    # note: need to read sequence files first to allow SateProducts to
    # correctly self-configure
    user_config.read_seq_filepaths(src=user_config.commandline.input,
            multilocus=user_config.commandline.multilocus)
    sate_products = filemgr.SateProducts(user_config)
    
    export_config_as_temp = True
    if export_config_as_temp:
        name_cfg = sate_products.get_abs_path_for_tag('sate_config.txt')
        command_line_group.exportconfig = None
        user_config.save_to_filepath(name_cfg)
        MESSENGER.send_info('Configuration written to "%s".\n' % name_cfg )
         

    MESSENGER.run_log_streams.append(sate_products.run_log_stream)
    MESSENGER.err_log_streams.append(sate_products.err_log_stream)
    temp_dir, temp_fs = run_sate_from_config(user_config, sate_products)
    _TIME_SPENT = time.time() - _START_TIME
    MESSENGER.send_info("Total time spent: %ss" % _TIME_SPENT)
    return True, temp_dir, temp_fs
