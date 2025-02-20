#
# Simulation class
#
# Author: Richard Plevin
#
# Copyright (c) 2022 the author and The Board of Trustees of the Leland Stanford Junior University.
# See LICENSE.txt for license details.
#
import json
import os
import pandas as pd
import traceback

from ..config import pathjoin
from ..core import OpgeeObject, split_attr_name, Timer
from ..error import OpgeeException, McsSystemError, McsUserError, CommandlineError
from ..log import getLogger
from ..model_file import ModelFile
from ..pkg_utils import resourceStream
from ..utils import mkdirs, removeTree
from .LHS import lhs
from .distro import get_frozen_rv

_logger = getLogger(__name__)


TRIAL_DATA_CSV = 'trial_data.csv'
RESULTS_CSV = 'results.csv'
FAILURES_CSV = 'failures.csv'
MODEL_FILE = 'merged_model.xml'
META_DATA_FILE = 'metadata.json'

DISTROS_CSV = 'mcs/etc/parameter_distributions.csv'

DEFAULT_DIGITS = 3

def magnitude(quantity, digits=DEFAULT_DIGITS):          # pragma: no cover
    return round(quantity.m, digits)

def model_file_path(sim_dir):     # pragma: no cover
    model_file = pathjoin(sim_dir, MODEL_FILE)
    return model_file

def read_distributions(pathname=None):
    """
    Read distributions from the designated CSV file. These are combined with those defined
    using the @Distribution.register() decorator, used to define distributions with dependencies.

    :param pathname: (str) the pathname of the CSV file describing parameter distributions
    :return: (none)
    """
    distros_csv = pathname or resourceStream(DISTROS_CSV, stream_type='bytes', decode=None)

    df = pd.read_csv(distros_csv, skip_blank_lines=True, comment='#').fillna('')

    for row in df.itertuples(index=False, name='row'):
        shape = row.distribution_type.lower()
        name = row.variable_name
        low = row.low_bound
        high = row.high_bound
        mean = row.mean
        stdev = row.SD
        default = row.default_value
        prob_of_yes = row.prob_of_yes
        pathname = row.pathname

        if name == '':
            continue

        if low == '' and high == '' and mean == '' and prob_of_yes == '' and shape != 'empirical':
            _logger.info(f"* {name} depends on other distributions / smart defaults")        # TODO add in lookup of attribute value
            continue

        if shape == 'binary':
            if prob_of_yes == 0 or prob_of_yes == 1:
                _logger.info(f"* Ignoring distribution on {name}, Binary distribution has prob_of_yes = {prob_of_yes}")
                continue

            rv = get_frozen_rv('weighted_binary', prob_of_one=0.5 if prob_of_yes == '' else prob_of_yes)

        elif shape == 'uniform':
            if low == high:
                _logger.info(f"* Ignoring distribution on {name}, Uniform high and low bounds are both {low}")
                continue

            rv = get_frozen_rv('uniform', min=low, max=high)

        elif shape == 'triangular':
            if low == high:
                _logger.info(f"* Ignoring distribution on {name}, Triangle high and low bounds are both {low}")
                continue

            rv = get_frozen_rv('triangle', min=low, mode=default, max=high)

        elif shape == 'normal':
            if stdev == 0.0:
                _logger.info(f"* Ignoring distribution on {name}, Normal has stdev = 0")
                continue

            if low == '' or high == '':
                rv = get_frozen_rv('normal', mean=mean, stdev=stdev)
            else:
                rv = get_frozen_rv('truncated_normal', mean=mean, stdev=stdev, low=low, high=high)

        elif shape == 'lognormal':
            if stdev == 0.0:
                _logger.info(f"* Ignoring distribution on {name}, Lognormal has stdev = 0")
                continue

            if low == '' or high == '':     # must specify both low and high
                rv = get_frozen_rv('lognormal', logmean=mean, logstdev=stdev)
            else:
                rv = get_frozen_rv('truncated_lognormal', logmean=mean, logstdev=stdev, low=low, high=high)

        elif shape == 'empirical':
            rv = get_frozen_rv('empirical', pathname=pathname, colname=name)

        else:
            raise McsSystemError(f"Unknown distribution shape: '{shape}'")

        # merge CSV-based distros with decorator-based ones
        Distribution(name, rv)


class Distribution(OpgeeObject):

    instances = {}

    def __init__(self, full_name, rv):
        self.full_name = full_name
        try:
            self.class_name, self.attr_name = split_attr_name(full_name)
        except OpgeeException as e:
            raise McsUserError(f"attribute name format is 'ATTR' (same as 'Field.ATTR) or 'CLASS.ATTR'; got '{full_name}'")

        self.rv = rv
        self.instances[full_name] = self

    @classmethod
    def distro_by_name(cls, name):
        return cls.instances.get(name)

    @classmethod
    def distributions(cls):
        """
        Return a list of the defined Distribution instances.

        :return: (list of opgee.mcs.Distribution) the instances
        """
        return cls.instances.values()

    def __str__(self):
        return f"<Distribution '{self.full_name}' = {self.rv}>"


class Simulation(OpgeeObject):
    # TBD: update this document string
    """
    ``Simulation`` represents the file and directory structure of a Monte Carlo simulation.
    Each simulation has an associated top-level directory which contains:

    - `metadata.json`: currently, only the analysis name is stored here, but more stuff later.

    - `{field_name}/trial_data.csv`: values drawn from parameter distributions, with each row
      representing a single trial, and each column representing the vector of values drawn for
      a single parameter. This file is created by the "gensim" sub-command.

    - `analysis_XXX.csv`: results for the analysis named `XXX`. Each column represents the
      results of a single output variable. Each row represents the value of all output variables
      for one trial of a single field. The field name is thus included in each row, allowing
      results for all fields in a single analysis to be stored in one file.

    - `trials`: a directory holding subdirectories for each trial, allowing each to be run
      independently (e.g., on a multi-core or cluster computer). The directory structure under
      ``trials`` comprises two levels of 3-digit values, which, when concatenated form the
      trial number. That is, trial 1,423 would be found in ``trials/001/423``. This allows
      up to 1 million trials while ensuring that no directory contains more than 1000 items.
      Limiting directory size improves performance.
    """
    def __init__(self, sim_dir, analysis_name=None, trials=0, field_names=None,
                 save_to_path=None, meta_data_only=False):

        if not os.path.isdir(sim_dir):
            raise McsUserError(f"Simulation directory '{sim_dir}' does not exist.")

        self.pathname = sim_dir
        self.model_file = model_file = model_file_path(sim_dir)
        self.model = None
        self.model_xml_string = None

        self.trial_data_df = None # loaded on demand by ``trial_data`` method.
        self.trials = trials

        self.analysis_name = analysis_name
        self.analysis = None
        self.field_names = field_names
        self.metadata = None

        if not analysis_name:
            self._load_meta_data(field_names)
            if meta_data_only:
                return

        try:
            _logger.debug(f"Caching file '{model_file}' as xml_string")
            with open(model_file) as f:
                self.model_xml_string = f.read()
        except Exception as e:
            raise McsSystemError(f"Failed to read model file '{model_file}' to XML string: {e}")

        # TBD: to allow the same trial_num to be run across fields, cache field
        #      trial_data in a dict by field name rather than a single DF

        if analysis_name:
            self._save_meta_data()
            if meta_data_only:
                return

        self.load_model(save_to_path=save_to_path)

        if trials > 0:
            self.generate()

    def load_model(self, save_to_path=None):
        """
        Loads the model (reading just the field being run by this Simulation) from XML
        to avoid carrying state between trials.

        :return: none
        """
        mf = ModelFile(self.model_file,
                       xml_string=self.model_xml_string,
                       use_default_model=False,
                       analysis_names=[self.analysis_name],
                       field_names=self.field_names,
                       save_to_path=save_to_path)
        self.model = mf.model

        self.analysis = self.model.get_analysis(self.analysis_name, raiseError=False)
        if not self.analysis:
            raise CommandlineError(f"Analysis '{self.analysis_name}' was not found in model")


    @classmethod
    def read_metadata(cls, sim_dir):
        """
        Used by runsim to get the field names without loading the whole simulation
        """
        sim = Simulation(sim_dir, meta_data_only=True)
        return sim.metadata

    def _save_meta_data(self):
        self.metadata = {
            'analysis_name': self.analysis_name,
            'trials'       : self.trials,
            'field_names'  : self.field_names,  # None => process all Fields in the Analysis
        }

        with open(self.metadata_path(), 'w') as fp:
            json.dump(self.metadata, fp, indent=2)

    def _load_meta_data(self, field_names):
        metadata_path = self.metadata_path()
        try:
            with open(metadata_path, 'r') as fp:
                self.metadata = metadata = json.load(fp)
        except Exception as e:
            raise McsUserError(f"Failed to load simulation '{metadata_path}' : {e}")

        if field_names:
            names = set(field_names)
            # Use list comprehension rather than set.intersection to maintain original order
            self.field_names = [name for name in metadata['field_names'] if name in names]
        else:
            self.field_names = metadata['field_names']

        self.analysis_name = metadata['analysis_name']
        self.trials        = metadata['trials']

    @classmethod
    def new(cls, sim_dir, model_files, analysis_name, trials,
            field_names=None, overwrite=False, use_default_model=True):
        """
        Create the simulation directory and the ``sandboxes`` sub-directory.

        :param sim_dir: (str) the top-level simulation directory
        :param model_files: (list of XML filenames) the XML files to load, in order to be merged
        :param analysis_name: (str) the name of the analysis for which to generate the MCS
        :param trials: (int) the number of trials to generate
        :param field_names: (list of str or None) Field names to limit the Simulation to use.
           (None => use all Fields defined in the Analysis.)
        :param overwrite: (bool) if True, overwrite directory if it already exists,
          otherwise refuse to do so.
        :param use_default_model: (bool) whether to use the default model in etc/opgee.xml as
           the baseline model to merge with.
        :return: a new ``Simulation`` instance
        """
        if os.path.lexists(sim_dir):
            if not overwrite:
                raise McsUserError(f"Directory '{sim_dir}' already exists. Use "
                                    "Simulation.new(sim_dir, overwrite=True) to replace it.")
            removeTree(sim_dir, ignore_errors=False)

        mkdirs(sim_dir)

        # Stores the merged model in the simulation folder to ensure the same one
        # is used for all trials. Avoids having each worker regenerate this, and
        # thus avoids different models being used if underlying files change while
        # the simulation is running.
        merged_model_file = model_file_path(sim_dir)
        mf = ModelFile(model_files, use_default_model=use_default_model, save_to_path=merged_model_file)

        analysis = mf.model.get_analysis(analysis_name, raiseError=False)
        if not analysis:
            raise McsUserError(f"Analysis '{analysis_name}' was not found in model")

        field_names = field_names or analysis.field_names(enabled_only=True)
        sim = cls(sim_dir, analysis_name=analysis_name, trials=trials, field_names=field_names)
        return sim

    def field_dir(self, field):
        d = pathjoin(self.pathname, field.name)
        return d

    def trial_data_path(self, field, mkdir=False):
        d = self.field_dir(field)
        if mkdir:
            mkdirs(d)
        path = pathjoin(d, TRIAL_DATA_CSV)
        return path

    def results_path(self, field, mkdir=False):
        d = self.field_dir(field)
        if mkdir:
            mkdirs(d)
        path = pathjoin(d, RESULTS_CSV)
        return path

    def failures_path(self, field):
        d = self.field_dir(field)
        path = pathjoin(d, FAILURES_CSV)
        return path

    def metadata_path(self):
        return pathjoin(self.pathname, META_DATA_FILE)

    def chosen_fields(self):
        a = self.analysis
        names = self.field_names
        fields = [a.get_field(name) for name in names] if names else a.fields()
        return fields

    def lookup(self, full_name, field):
        class_name, attr_name = split_attr_name(full_name)

        if class_name is None or class_name == 'Field':
            obj = field

        elif class_name == 'Analysis':
            obj = self.analysis

        else:
            obj = field.find_process(class_name)
            if obj is None:
                raise McsUserError(f"A process of class '{class_name}' was not found in {field}")

        attr_obj = obj.attr_dict.get(attr_name)
        if attr_obj is None:
            raise McsUserError(f"The attribute '{attr_name}' was not found in '{obj}'")

        return attr_obj

    # TBD: need a way to specify correlations
    def generate(self, corr_mat=None):
        """
        Generate simulation data for the given ``Analysis``.

        :param corr_mat: a numpy matrix representing the correlation
           between each pair of parameters. corrMat[i,j] gives the
           desired correlation between the i'th and j'th entries of
           the parameter list.
        :return: none
        """
        trials = self.trials

        for field in self.chosen_fields():
            cols = []
            rv_list = []
            distributions = Distribution.distributions()

            for dist in distributions:
                target_attr = self.lookup(dist.full_name, field)

                # If the object has an explicit value for an attribute, we ignore the distribution
                if target_attr.explicit:
                    _logger.debug(f"{field} has an explicit value for '{dist.attr_name}'; ignoring distribution")
                    continue

                rv_list.append(dist.rv)
                cols.append(dist.attr_name if dist.class_name == 'Field' else dist.full_name)

            if not cols:
                raise McsUserError(f"Can't run MCS: all parameters with distributions have explicit values in {field}.")

            self.trial_data_df = df = lhs(rv_list, trials, columns=cols, corrMat=corr_mat)
            df.index.name = 'trial_num'
            self.save_trial_data(field)

    def save_trial_data(self, field):
        filename = self.trial_data_path(field, mkdir=True)
        _logger.info(f"Writing '{filename}'")
        self.trial_data_df.to_csv(filename)

    def save_trial_results(self, field, df, failures):
        filename = self.results_path(field, mkdir=True)
        _logger.info(f"Writing '{filename}'")
        df.to_csv(filename, index=False)

        # Save info on failed trials, too
        failures_csv = self.failures_path(field)
        _logger.info(f"Writing {len(failures)} failures to '{failures_csv}'")
        with open(failures_csv, 'w') as f:
            f.write("trial_num,message\n")
            for trial_num, msg in failures:
                f.write(f'{trial_num},"{msg}"\n')

    def field_trial_data(self, field):
        """
        Read the trial data CSV from the top-level directory and return the DataFrame.
        The data is cached in the ``Simulation`` instance for re-use.

        :param field: (opgee.Field  or str) a field instance or name to read data for
        :return: (pd.DataFrame) the values drawn for each field, parameter, and trial.
        """
        # TBD: allow option of using same draws across fields?

        if isinstance(field, str):
            field = self.analysis.get_field(field)

        if self.trial_data_df is not None:
            return self.trial_data_df

        path = self.trial_data_path(field)
        if not os.path.lexists(path):
            raise McsSystemError(f"Can't read trial data: '{path}' doesn't exist.")

        try:
            df = pd.read_csv(path, index_col='trial_num')

        except Exception as e:
            raise McsSystemError(f"Can't read trial data from '{path}': {e}")

        self.trial_data_df = df
        return df

    def trial_data(self, field, trial_num):
        """
        Return the values for all parameters for trial ``trial_num``.

        :param trial_num: (int) trial number
        :return: (pd.Series) the values for all parameters for the given trial.
        """
        df = self.field_trial_data(field)  # load data file on demand

        if trial_num not in df.index:
            path = self.trial_data_path(field)
            raise McsSystemError(f"Trial {trial_num} was not found in '{path}'")

        s = df.loc[trial_num]
        return s

    def set_trial_data(self, field, trial_num):
        _logger.debug(f"set_trial_data for trial {trial_num})")
        data = self.trial_data(field, trial_num)

        for name, value in data.items():
            attr = self.lookup(name, field)
            attr.explicit = True
            attr.set_value(value)

            # Debugging only
            # if name == 'WOR' and value == 0:
            #     pass

    def run_field(self, field, trial_nums=None):
        """
        Run the Monte Carlo simulation for the given field and trial numbers.

        :param field: (opgee.Field) the Field to evaluate in MCS
        :param trial_nums: (iterator of ints) the trial numbers to run, or
           ``None`` to run all trials.
        :return: (int) the number of successfully run trials
        """

        trial_nums = range(self.trials) if trial_nums is None else trial_nums

        completed = 0
        results = []
        failures = []

        for trial_num in trial_nums:
            try:
                # Reload from cached XML string to avoid stale state
                self.load_model()

                # Use the new instance of field from the reloaded model
                field = self.analysis.get_field(field.name)

                self.set_trial_data(field, trial_num)

                # TBD: test re-running
                #    SmartDefault.apply_defaults(field, analysis=self.analysis)
                #  however, changes to structure are not applied unless we call
                #  the function field.finalize_process_graph(), which also calls
                #  SmartDefault.apply_defaults() and resolve_process_choices(),
                #  and resets the field's network graph.

                field.run(self.analysis, compute_ci=True, trial_num=trial_num)
                # field.report()
                completed += 1

            except Exception as e:
                failures.append((trial_num, e))
                _logger.warning(f"Exception raised in trial {trial_num} in {field}: {e}")
                _logger.debug(traceback.format_exc())
                continue

            # The following would exit the trial loop, so probably better to skip & continue
            # except OpgeeException as e:
            #     raise TrialErrorWrapper(e, trial_num)

            ci = field.carbon_intensity     # computed and saved in field.run()

            # energy = field.energy.data
            emissions = field.emissions.data

            ghg = emissions.loc['GHG']
            total_ghg = ghg.sum()
            vff = ghg['Venting'] + ghg['Flaring'] + ghg['Fugitives']

            tup = (trial_num,
                   magnitude(ci),
                   magnitude(total_ghg),
                   magnitude(ghg['Combustion']),
                   magnitude(ghg['Land-use']),
                   magnitude(vff),
                   magnitude(ghg['Other']))

            results.append(tup)

        cols = ['trial_num',
                'CI',
                'total_GHG',
                'combustion',
                'land_use',
                'VFF',
                'other']

        df = pd.DataFrame.from_records(results, columns=cols)
        self.save_trial_results(field, df, failures)

        return completed

    def run(self, trial_nums, field_names=None):
        """
        Run the given Monte Carlo trials for ``analysis``. If ``fields`` is
        ``None``, all fields are run, otherwise, only the indicated fields are
        run.

        :param trial_nums: (list of int) trials to run. ``None`` implies all trials.
        :param field_names: (list of str) names of fields to run
        :return: none
        """
        timer = Timer('Simulation.run').start()

        ana = self.analysis
        fields = [ana.get_field(name) for name in field_names] if field_names else self.chosen_fields()

        for field in fields:
            if field.is_enabled():
                self.run_field(field, trial_nums)
            else:
                _logger.info(f"Ignoring disabled {field}")

        _logger.info(timer.stop())

        # results for a field in `analysis`. Each column represents the results
        # of a single output variable. Each row represents the value of all output
        # variables for one trial of a single field. The field name is thus
        # included in each row, allowing results for all fields in a single
        # analysis to be stored in one file.
