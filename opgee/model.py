#
# Model class
#
# Author: Richard Plevin and Wennan Long
#
# Copyright (c) 2021-2022 The Board of Trustees of the Leland Stanford Junior University.
# See LICENSE.txt for license details.
#
import pint

from . import ureg
from .analysis import Analysis
from .container import Container
from .core import elt_name, magnitude, instantiate_subelts
from .error import OpgeeException, CommandlineError
from .field import Field
from .log import getLogger
from .table_manager import TableManager
from .table_update import TableUpdate

DEFAULT_SCHEMA_VERSION = "4.0.0.a"

_logger = getLogger(__name__)

class Model(Container):

    def __init__(self, name, attr_dict=None, table_updates=None):
        super().__init__(name, attr_dict=attr_dict, parent=None)

        Model.instance = self
        self.schema_version = attr_dict.get('schema_version', DEFAULT_SCHEMA_VERSION)

        # These are set in from_xml after instantiation
        self.analysis_dict = None
        self.field_dict = None

        self.pathnames = None  # set by calling set_pathnames(path)

        # parameters controlling process cyclic calculations
        self.maximum_iterations = self.attr('maximum_iterations')
        self.maximum_change = self.attr('maximum_change')

        self.table_mgr = tbl_mgr = TableManager(updates=table_updates)

        # load all the GWP options
        df = tbl_mgr.get_table('GWP')

        self.gwp_horizons = list(df.Years.unique())
        self.gwp_versions = list(df.columns[2:])
        self.gwp_dict = {y: df.query('Years == @y').set_index('Gas', drop=True).drop('Years', axis='columns') for y in
                         self.gwp_horizons}

        constants_df = tbl_mgr.get_table('constants')
        self.constants = {name: ureg.Quantity(float(row.value), row.unit) for name, row in constants_df.iterrows()}

        # TODO: to support PRELIM, we might want a way to handle these that is less model-specific
        #  Perhaps separate namespaces for each model, like
        #  self.table_dict = {'OPGEE' : OpgeeTables(tbl_mgr), 'PRELIM' : PrelimTables(tbl_mgr)}
        #  Then all the OPGEE-specific instance vars or pushed down into an OpgeeTables instance

        self.vertical_drill_df = tbl_mgr.get_table("vertical-drilling-energy-intensity")
        self.horizontal_drill_df = tbl_mgr.get_table("horizontal-drilling-energy-intensity")
        self.fracture_energy = tbl_mgr.get_table("fracture-consumption-table")
        self.land_use_EF = tbl_mgr.get_table("land-use-EF")

        self.process_EF_df = tbl_mgr.get_table("process-specific-EF")

        self.imported_gas_comp = tbl_mgr.get_table("imported-gas-comp")

        self.water_treatment = tbl_mgr.get_table("water-treatment")

        self.heavy_oil_upgrading = tbl_mgr.get_table("heavy-oil-upgrading")

        self.transport_parameter = tbl_mgr.get_table("transport-parameter")
        self.transport_share_fuel = tbl_mgr.get_table("transport-share-fuel")
        self.transport_by_mode = tbl_mgr.get_table("transport-by-mode")

        self.mining_energy_intensity = tbl_mgr.get_table("bitumen-mining-energy-intensity")

        self.prod_combustion_coeff = tbl_mgr.get_table("product-combustion-coeff")
        self.reaction_combustion_coeff = tbl_mgr.get_table("reaction-combustion-coeff")

        self.gas_turbine_tbl = tbl_mgr.get_table("gas-turbine-specs")

        self.gas_dehydration_tbl = tbl_mgr.get_table("gas-dehydration")
        self.AGR_tbl = tbl_mgr.get_table("acid-gas-removal")
        self.ryan_holmes_process_tbl = tbl_mgr.get_table("ryan-holmes-process")
        self.demethanizer = tbl_mgr.get_table("demethanizer")
        self.upstream_CI = tbl_mgr.get_table("upstream-CI")

        self.pubchem_cid = tbl_mgr.get_table("pubchem-cid")

        # tables for the fugitive model
        self.loss_matrix_gas = tbl_mgr.get_table("loss-matrix-gas")
        self.loss_matrix_oil = tbl_mgr.get_table("loss-matrix-oil")
        self.productivity_gas = tbl_mgr.get_table("productivity-gas")
        self.productivity_oil = tbl_mgr.get_table("productivity-oil")

        self.site_fugitive_processing_unit_breakdown = tbl_mgr.get_table("site-fugitive-processing-unit-breakdown")
        self.well_completion_and_workover_C1_rate = tbl_mgr.get_table("well-completion-and-workover-C1-rate")

        # TBD: should these be settable per Analysis?
        # parameters controlling process cyclic calculations
        self.maximum_iterations = self.attr('maximum_iterations')
        self.maximum_change = self.attr('maximum_change')

        self.pathnames = None  # set by calling set_pathnames(path)
        # TBD: apply table updates

    def set_pathnames(self, pathnames):
        self.pathnames = pathnames

    def fields(self):
        return self.field_dict.values()

    def analyses(self):
        return self.analysis_dict.values()

    def get_analysis(self, name, raiseError=True) -> Analysis:
        analysis = self.analysis_dict.get(name)
        if analysis is None and raiseError:
            raise OpgeeException(f"Analysis named '{name}' is not defined")

        return analysis

    def get_field(self, name, raiseError=True) -> Field:
        field = self.field_dict.get(name)
        if field is None and raiseError:
            raise OpgeeException(f"Field named '{name}' is not defined in Model")

        return field

    def const(self, name):
        """
        Return the value of a constant declared in tables/constants.csv

        :param name: (str) name of constant
        :return: (float with unit) value of constant
        """
        try:
            return self.constants[name]
        except KeyError:
            raise OpgeeException(f"No known constant with name '{name}'")

    def _children(self, include_disabled=False):
        """
        Return a list of all children objects. External callers should use children()
        instead, as it respects the self.is_enabled() setting.
        """
        return self.analyses()  # N.B. returns an iterator


    def partial_ci_values(self, analysis, field, nodes):
        """
        Compute partial CI for each node in ``nodes``, skipping boundary nodes, since
        these have no emissions and serve only to identify the endpoint for CI calculation.

        :param analysis: (opgee.Analysis)
        :param field: (opgee.Field)
        :param nodes: (list of Processes and/or Containers)
        :return: A list of tuples of (item_name, partial_CI)
        """
        from .error import ZeroEnergyFlowError
        from .process import Boundary

        try:
            energy = field.boundary_energy_flow_rate(analysis)

        except ZeroEnergyFlowError:
            _logger.error(f"Can't save results: zero energy flow at system boundary for {field}")
            return None

        def partial_ci(obj):
            ghgs = obj.emissions.data.sum(axis='columns')['GHG']
            if not isinstance(ghgs, pint.Quantity):
                ghgs = ureg.Quantity(ghgs, "tonne/day")

            ci = ghgs / energy
            # convert to g/MJ, but we don't need units in CSV file
            return ci.to("grams/MJ").m

        results = [(obj.name, partial_ci(obj)) for obj in nodes if not isinstance(obj, Boundary)]
        return results

    def save_results(self, tuples, csvpath, by_process=False):
        """
        Save the carbon intensity (CI) results for the indicated (field, analysis)
        tuples to the indicated CSV pathname, ``csvpath``. By default, results are
        written for top-level processes and aggregators. If ``by_process`` is True,
        the results are written out for all processes, ignoring aggregators.

        :param tuples: (sequence of tuples of (analysis, field) instances)
        :param by_process: (bool) if True, write results by process. If False,
            write results for top-level processes and aggregators only.
        :return: none
        """
        import pandas as pd

        rows = []

        for (field, analysis) in tuples:
            nodes = field.processes() if by_process else field.children()
            ci_tuples = self.partial_ci_values(analysis, field, nodes)

            fld_name = field.name
            ana_name = analysis.name

            rows.append({'analysis': ana_name,
                         'field': fld_name,
                         'node': 'TOTAL',
                         'CI': field.carbon_intensity.m})

            # ignore failed fields
            if ci_tuples is not None:
                for name, ci in ci_tuples:
                    rows.append({'analysis' : ana_name,
                                 'field' : fld_name,
                                 'node' : name,
                                 'CI' : ci})

        df = pd.DataFrame(data=rows)
        _logger.info(f"Writing '{csvpath}'")
        df.to_csv(csvpath, index=False)

    def save_for_comparison(self, tuples, csvpath):
        import pandas as pd

        energy_cols = []
        emission_cols = []

        def total_emissions(proc, gwp):
            rates = proc.emissions.rates(gwp)
            total = rates.loc["GHG"].sum()
            return magnitude(total)

        for (field, analysis) in tuples:
            procs = field.processes()
            energy_by_proc = {proc.name: magnitude(proc.energy.rates().sum()) for proc in procs}
            s = pd.Series(energy_by_proc, name=field.name)
            energy_cols.append(s)

            emissions_by_proc = {proc.name: total_emissions(proc, analysis.gwp) for proc in procs}
            s = pd.Series(emissions_by_proc, name=field.name)
            emission_cols.append(s)

        def _save(columns, csvpath):
            df = pd.concat(columns, axis='columns')
            df.index.name = 'process'
            df.sort_index(axis='rows', inplace=True)

            _logger.info(f"Writing '{csvpath}'")
            df.to_csv(csvpath)

        _save(energy_cols, "energy-" + csvpath)
        _save(emission_cols, "emissions-" + csvpath)

    @classmethod
    def from_xml(cls, elt, parent=None, analysis_names=None, field_names=None):
        """
        Instantiate an instance from an XML element

        :param elt: (etree.Element) representing a <Model> element
        :param parent: (None) this argument should be ``None`` for Model instances.
        :param field_names: (list of str) the names of fields to include. Any other
          fields are ignored when building the model from the XML.
        :return: (Model) instance populated from XML
        """
        attr_dict = cls.instantiate_attrs(elt)
        table_updates = instantiate_subelts(elt, TableUpdate, as_dict=True)

        model = Model(elt_name(elt), attr_dict=attr_dict, table_updates=table_updates)

        fields = instantiate_subelts(elt, Field, parent=model, include_names=field_names)
        if field_names and not fields:
            raise CommandlineError(f"Indicated field names {field_names} were not found in model")

        model.field_dict = model.adopt(fields, asDict=True)

        analyses = instantiate_subelts(elt, Analysis, parent=model, include_names=analysis_names)
        if analysis_names and not analyses:
            raise CommandlineError(f"Specified analyses {analysis_names} not found in model")

        model.analysis_dict = model.adopt(analyses, asDict=True)

        if field_names:
            for analysis in analyses:
                analysis.restrict_fields(field_names)

        return model
