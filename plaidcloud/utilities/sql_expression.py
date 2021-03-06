#!/usr/bin/env python
# coding=utf-8

"""Utility library for sqlalchemy metaprogramming used in analyze transforms"""

from __future__ import absolute_import
from __future__ import division

import re
import uuid

from toolz.functoolz import juxt, compose
from toolz.functoolz import identity as ident
from toolz.dicttoolz import merge

import sqlalchemy
import sqlalchemy.orm


from plaidcloud.rpc.type_conversion import sqlalchemy_from_dtype
from plaidcloud.utilities.stringtransforms import apply_variables
from plaidcloud.utilities import sqlalchemy_functions as sf  # Not unused import, it creates the SQLalchemy functions used


__author__ = 'Adams Tower'
__maintainer__ = 'Adams Tower <adams.tower@tartansolutions.com>'
__copyright__ = '© Copyright 2017-2020, Tartan Solutions, Inc'
__license__ = 'Apache 2.0'

# TODO: move transform functions here, document them, and refactor their api
# TODO: write unit tests

MAGIC_COLUMN_MAPPING = {
    'path': u':::DOCUMENT_PATH:::',
    'file_name': u':::FILE_NAME:::',
    'tab_name': u':::TAB_NAME:::',
    'last_modified': u':::LAST_MODIFIED:::',
}

CSV_TYPE_DELIMITER = '::'
SCHEMA_PREFIX = 'anlz'
table_dot_column_regex = re.compile(r'^table(\d*)\..*')


class SQLExpressionError(Exception):
    # Will typically be caught by
    # workflow_runner.function.utility.transform_handler and converted into a
    # UserError
    pass


def eval_expression(expression, variables, tables, extra_keys=None, disable_variables=False, table_numbering_start=1):
    safe_dict = get_safe_dict(tables, extra_keys, table_numbering_start=table_numbering_start)
    if disable_variables:
        expression_with_variables = expression
    else:
        expression_with_variables = apply_variables(expression, variables)
    compiled_expression = compile(
        expression_with_variables,
        '<string>',
        'eval',
        division.compiler_flag,
    )
    try:
        return eval(compiled_expression, safe_dict)
    except Exception as e:
        message = str(e)
        raise SQLExpressionError(
            'Error in expression:\n'
            + '    {}\n'.format(expression)
            + message
        )


def on_clause(table_a, table_b, join_map, special_null_handling=False):
    """ Given two analyze tables, and a map of join keys with the structure:
    [{'a_column': COLUMN_NAME, 'b_column: COLUMN_NAME}...]
    returns a sqlalchemy clause filtering on those join keys, suitable for
    passing to sqlalchemy.join

    If special_null_handling is set to True, it will generate a clause suitable
    for a WHERE clause, so that it can be used in an anti-join subquery for
    example. Specifically, it will have extra checks to join on null columns.

    Example:

        join_query = select_query.select_from(sqlalchemy.join(
            table_a, table_b,
            on_clause(table_a, table_b, self.config['join_map'])
        ))

    """

    def column_a(jm):
        return next((c for c in table_a.columns if c.name == jm['a_column']), getattr(table_a.columns, jm['a_column']))

    def column_b(jm):
        return next((c for c in table_b.columns if c.name == jm['b_column']), getattr(table_b.columns, jm['b_column']))

    def column_a_equals_column_b(jm):
        return column_a(jm) == column_b(jm)

    def both_columns_null(jm):
        return sqlalchemy.and_(
            column_a(jm).is_(None),
            column_b(jm).is_(None),
        )

    def my_or(lst):
        # Calls sqlalchemy.or_ on a single argument list instead of an *args list
        return sqlalchemy.or_(*lst)

    if special_null_handling:
        column_join_expression = compose(my_or, juxt(
            column_a_equals_column_b,
            both_columns_null,
        ))
    else:
        column_join_expression = column_a_equals_column_b

    return sqlalchemy.and_(*[
        column_join_expression(jm)
        for jm in join_map
    ])


def get_column_table(source_tables, target_column_config, source_column_configs, table_numbering_start=1):
    """Find the source table associated with a column."""

    if len(source_tables) == 1:  # Shortcut for most simple cases
        return source_tables[0]

    if target_column_config.get('source_table'):
        if target_column_config['source_table'].lower() in ('table a', 'table1'):
            return source_tables[0]
        elif target_column_config['source_table'].lower() in ('table b', 'table2'):
            return source_tables[1]

    source_name = target_column_config['source']

    match = table_dot_column_regex.match(source_name)

    if match:  # They gave us a table number. Subtract 1 to find it's index
        table_number = int(match.groups()[0])
        return source_tables[table_number - table_numbering_start]

    elif source_name.startswith('table.'):  # special case for just 'table'
        return source_tables[0]

    else:
        # None of our shortcuts worked, so look for the first table to have a
        # column of that name.
        for table, columns in zip(source_tables, source_column_configs):
            columnset = set([c['source'] for c in columns])
            if source_name in columnset:
                return table

        # If nothing found at all:
        raise SQLExpressionError("Mapped source column {} is not in any source tables.".format(source_name))


def get_from_clause(
        tables, target_column_config, source_column_configs, aggregate=False,
        sort=False, variables=None, cast=True, disable_variables=False, table_numbering_start=1,
    ):
    """Given info from a config, returns a sqlalchemy from clause."""

    expression = target_column_config.get('expression')
    constant = target_column_config.get('constant')
    source = target_column_config.get('source')
    name = target_column_config.get('target')
    type_ = sqlalchemy_from_dtype(target_column_config.get('dtype'))
    if cast:
        # This is only used for standard source->target columns, because expressions depend on them
        cast_fn = sqlalchemy.cast
    else:
        def cast_fn(col, type_): return col

    if aggregate:
        agg_fn = get_agg_fn(target_column_config.get('agg'))
    else:
        agg_fn = ident

    if sort and 'sort' in target_column_config:
        if target_column_config['sort']['ascending']:
            sort_fn = sqlalchemy.asc
        else:
            sort_fn = sqlalchemy.desc
    else:
        sort_fn = ident

    if variables is None:
        variables = {}

    if constant:
        # Agg_fn is ignored, and we wrap in sqlalchemy.literal
        # So is cast
        if disable_variables:
            def av(x, _): return x
        else:
            av = apply_variables
        return sort_fn(
            sqlalchemy.cast(
                sqlalchemy.literal(
                    av(constant, variables),
                    type_=type_,
                ),
                type_=type_,
            )
        ).label(name)

    elif expression:
        return sort_fn(
            sqlalchemy.cast(
                agg_fn(
                    eval_expression(
                        expression.strip(), variables, tables, disable_variables=disable_variables,
                        table_numbering_start=table_numbering_start,
                    )
                ),
                type_=type_,
            )
        ).label(name)

    elif source:
        table = get_column_table(tables, target_column_config, source_column_configs, table_numbering_start=table_numbering_start)
        if '.' in source:
            source_without_table = source.split('.', 1)[1]
        else:
            source_without_table = source  # a couple extra checks, in an error scenario, but flatter code
        if target_column_config.get('agg') == 'count_null':
            col = None
        elif source in table.columns:
            col = table.columns[source]
        elif source_without_table in table.columns:
            col = table.columns[source_without_table]
        else:
            raise SQLExpressionError(f'Cannot find source column {source} in table {table.name}')

        return sort_fn(
            cast_fn(
                agg_fn(col),
                type_=type_,
            )
        ).label(name)

    elif target_column_config.get('dtype') in ('serial', 'bigserial'):
        return None

    else:
        raise SQLExpressionError('Target Column {} needs either a Constant, an Expression or a Source Column!'.format(
            target_column_config.get('target')
        ))


def get_project_schema(project_id=None):
    schema = project_id
    if not schema.startswith(SCHEMA_PREFIX):
        schema = '{}{}'.format(SCHEMA_PREFIX, schema)
    return schema


def get_agg_fn(agg_str):
    """Mapping of aggregation strings to aggregation functions.
       Aggregation strings ending in '_null' will include nulls, but will resolve to the same aggregation name.
    """
    if agg_str is None:
        return ident
    elif agg_str.endswith('_null'):
        return get_agg_fn(agg_str[:-5])
    elif agg_str in ['group', 'dont_group']:
        return ident
    else:
        try:
            return getattr(sqlalchemy.func, agg_str)
        except Exception:
            return ident


class Result(object):
    # TODO: figure out what this is for. It looks like it's similar to a
    #  sqlalchemy result object, and it's being made available to the user when
    #  they're writing a HAVING clause.

    def __init__(
            self, tables, target_columns, source_column_configs,
            aggregate=False, sort=False, variables=None, table_numbering_start=1):

        self.__dict__ = {
            tc['target']: get_from_clause(
                tables, tc, source_column_configs, aggregate, sort,
                variables=variables or {}, table_numbering_start=table_numbering_start,
            )
            for tc in target_columns
            if tc['dtype'] not in ('serial', 'bigserial')
        }

        
def get_safe_dict(tables, extra_keys=None, table_numbering_start=1):
    """Returns a dict of 'builtins' and table accessor variables for user
    written expressions."""
    if extra_keys is None:
        extra_keys = {}

    def get_column(table, col):
        if col in table:
            return table[col]
        else:
            # Obtaining the table here would be really ugly. table refers to a
            # table.columns object. We could maybe change it to some extension of whatever the table.columns object is
            raise SQLExpressionError(
                'Could not run get_column: column {col} does not exist.'.format(
                    col=repr(col),
                )
            )

    default_keys = {
        'sqlalchemy': sqlalchemy,
        'table': tables[0].columns if tables else None,
        'and_': sqlalchemy.and_,
        'or_': sqlalchemy.or_,
        'not_': sqlalchemy.not_,
        'cast': sqlalchemy.cast,
        'case': sqlalchemy.case,
        'Null': None,
        'null': None,
        'NULL': None,
        'true': True,
        'TRUE': True,
        'false': False,
        'FALSE': False,
        'get_column': get_column,
        # 'func': FuncPlus(),
        'func': sqlalchemy.func,
        'value': sqlalchemy.literal,
        'v': sqlalchemy.literal,
        'bigint': sqlalchemy.BIGINT,
        'Bigint': sqlalchemy.BIGINT,
        'BIGINT': sqlalchemy.BIGINT,
        'float': sqlalchemy.Float,
        'Float': sqlalchemy.Float,
        'FLOAT': sqlalchemy.Float,
        'integer': sqlalchemy.INTEGER,
        'Integer': sqlalchemy.INTEGER,
        'INTEGER': sqlalchemy.INTEGER,
        'smallint': sqlalchemy.SMALLINT,
        'Smallint': sqlalchemy.SMALLINT,
        'SMALLINT': sqlalchemy.SMALLINT,
        'text': sqlalchemy.TEXT,
        'Text': sqlalchemy.TEXT,
        'TEXT': sqlalchemy.TEXT,
        'boolean': sqlalchemy.BOOLEAN,
        'Boolean': sqlalchemy.BOOLEAN,
        'BOOLEAN': sqlalchemy.BOOLEAN,
        'numeric': sqlalchemy.NUMERIC,
        'Numeric': sqlalchemy.NUMERIC,
        'NUMERIC': sqlalchemy.NUMERIC,
        'timestamp': sqlalchemy.TIMESTAMP,
        'Timestamp': sqlalchemy.TIMESTAMP,
        'TIMESTAMP': sqlalchemy.TIMESTAMP,
        'interval': sqlalchemy.Interval,
        'Interval': sqlalchemy.Interval,
        'INTERVAL': sqlalchemy.Interval,
        'date': sqlalchemy.Date,
        'Date': sqlalchemy.Date,
        'DATE': sqlalchemy.Date,
        'time': sqlalchemy.Time,
        'Time': sqlalchemy.Time,
        'TIME': sqlalchemy.Time,
        'binary': sqlalchemy.LargeBinary,
        'Binary': sqlalchemy.LargeBinary,
        'BINARY': sqlalchemy.LargeBinary,
        'largebinary': sqlalchemy.LargeBinary,
        'Largebinary': sqlalchemy.LargeBinary,
        'LargeBinary': sqlalchemy.LargeBinary,
        'LARGEBINARY': sqlalchemy.LargeBinary,
    }

    # Generate table1, table2, ...
    table_keys = {
        'table{}'.format(i): table.columns
        for i, table in enumerate(tables, start=table_numbering_start)
    }

    return merge(default_keys, table_keys, extra_keys)


def get_table_rep(table_id, columns, schema, metadata=None, column_key='source', alias=None):
    """
    Returns:
        sqlalchemy.Table: object representing an analyze table
    Args:
        table_id (str): the name of the table in the database
        columns: a list of dicts (in transform config style) columns in the analyze table
        schema (str): the schema of the table
        metadata (sqlalchemy.MetaData): a sqlalchemy metadata object, to keep this table representation connected to others
        column_key (str): the key in each column dict under which to look for the column name
        alias (str, optional): If supplied, the SQL query will use the alias to make more human readable
    """
    if not table_id:
        raise SQLExpressionError('Cannot create sqlalchemy representation of a table without a table name.')

    if not metadata:
        metadata = sqlalchemy.MetaData()

    table = sqlalchemy.Table(
        table_id,
        metadata,
        *[
            sqlalchemy.Column(
                sc[column_key],
                sqlalchemy_from_dtype(sc['dtype']),
            )
            for sc in columns
        ],
        schema=schema,
        extend_existing=True  # If this is the second object representing this
                              # table, update.
                              # If you made it with this function, it should
                              # be no different.
    )

    if alias:
        return sqlalchemy.orm.aliased(table, name=alias)

    return table


def get_table_rep_using_id(table_id, columns, project_id, metadata, column_key='source', alias=None):
    """
    Returns:
        sqlalchemy.Table: object representing an analyze table
    Args:
        table_id (str): the id of the analyze table
        columns: a list of dicts (in transform config style) columns in the analyze table
        project_id (str): the project id of the project containing the analyze table
        metadata (sqlalchemy.MetaData): a sqlalchemy metadata object, to keep this table representation connected to others
        column_key (str): the key in each column dict under which to look for the column name
        alias (str, optional): If supplied, the SQL query will use the alias to make more human readable
    """

    schema = get_project_schema(project_id)

    return get_table_rep(table_id, columns, schema, metadata, column_key, alias)


def simple_select_query(config, project, metadata, variables):
    """Returns a select query from a single extract config, with a single
    source table, and standard key names."""
    if variables is None:
        variables = {}

    # Make a sqlalchemy representation of the source table
    from_table = get_table_rep_using_id(
        config['source'], config['source_columns'],
        project, metadata, alias=config.get('source_alias')
    )

    # Figure out select query
    select_query = get_select_query(
        tables=[from_table],
        source_columns=[config['source_columns']],
        target_columns=config['target_columns'],
        wheres=[config.get('source_where')],
        config=config, variables=variables,
    )

    return select_query


def modified_select_query(config, project, metadata, fmt=None, mapping_fn=None, variables=None):
    """Similar to simple_select_query, but accepts a config with consistently
    modified keys. E.g., source_b, source_columns_b, aggregate_b, etc.

    Can be provided with either a fmt string, like '{}_b', or a mapping_fn,
    like lambda x: x.upper(). In either case, the fmt or mapping_fn should
    convert the standard key (e.g. 'source') to the unusual key (e.g. 'source_b').

    If a modified key is not found in the config, it will default to the value
    of the unmodified key. E.g., if there's no 'source_b', it will take
    'source'. After that it will use the normal default value if there is one.

    If both a mapping_fn and a fmt are provided, fmt will be ignored, and
    mapping_fn will be used.
    """

    # The args we'll be modifying and searching the config for.
    required_args = (
        'source', 'source_columns', 'target_columns', 'source_where',
        'aggregate', 'having', 'use_target_slicer', 'limit_target_start',
        'limit_target_end', 'distinct', 'source_alias'
    )

    # If there's no mapping_fn, turn fmt into a mapping_fn.
    if mapping_fn is None:
        if fmt is None:
            raise SQLExpressionError("modified_select_query must be called with either a"
                                     " fmt or a mapping_fn!")
        else:
            # A function that formats a string with the provided fmt.
            def format_with_fmt(s): return fmt.format(s)
            mapping_fn = format_with_fmt

    if variables is None:
        variables = {}

    # Generate a fake config, taking the value of the modified keys from the
    # original config, or if those don't exist the value of the regular keys
    # from the original config.
    cleaned_config = {
        arg: config.get(mapping_fn(arg), config.get(arg))
        for arg in required_args
    }
    # Remove missing values, so they'll use defaults or error correctly deeper
    # in.
    cleaned_config = {k: v for k, v in cleaned_config.items() if v is not None}

    return simple_select_query(cleaned_config, project, metadata, variables)


def get_select_query(
        tables, source_columns, target_columns, wheres, config=None,
        variables=None, aggregate=None, having=None, use_target_slicer=None,
        limit_target_start=None, limit_target_end=None, distinct=None,
        count=None, disable_variables=None, table_numbering_start=1,
    ):
    """Returns a sqlalchemy select query from table objects and an extract
    config (or from the individual parameters in that config). tables,
    source_columns, and wheres should be lists, so that multiple tables can be
    joined. If they have more than one element, tables[n] corresponds to
    source_columns[n] corresponds to wheres[n].

    Args:
        tables:
        source_columns:
        target_columns:
        wheres:
        config:
        variables:
        aggregate:
        having:
        use_target_slicer:
        limit_target_start:
        limit_target_end:
        distinct:

    Returns:

    """

    if config is None:
        config = {}
    if variables is None:
        variables = {}
    # Ugh, there's really no way to write a macro to do this, except exec
    if aggregate is None:
        aggregate = config.get('aggregate', False)
    if having is None:
        having = config.get('having', None)
    if use_target_slicer is None:
        use_target_slicer = config.get('use_target_slicer', False)
    if limit_target_start is None:
        limit_target_start = config.get('limit_target_start', 0)
    if limit_target_end is None:
        limit_target_end = config.get('limit_target_end', 0)
    if distinct is None:
        distinct = config.get('distinct', False)
    if count is None:
        count = config.get('count', False)
    if disable_variables is None:
        disable_variables = config.get('disable_variables', False)

    # Build SELECT x FROM y section of our select query
    if count:
        # Much simpler for one table.
        # TODO: figure out how to do this for more than one table
        select_query = sqlalchemy.select([sqlalchemy.func.count()]).select_from(tables[0])
    else:
        column_select = [
            get_from_clause(
                tables,
                tc,
                source_columns,
                aggregate,
                variables=variables,
                disable_variables=disable_variables,
                table_numbering_start=table_numbering_start,
            )
            for tc in target_columns
            if tc['dtype'] not in ('serial', 'bigserial')
        ]

        select_query = sqlalchemy.select(column_select)

    # Build WHERE section of our select query
    wheres = [w for w in wheres if w] if wheres else []
    if wheres:
        combined_wheres = get_combined_wheres(
            wheres, tables, variables, disable_variables, table_numbering_start=table_numbering_start
        )
        select_query = select_query.where(sqlalchemy.and_(*combined_wheres))

    # Find any columns for sorting
    columns_to_sort_on = [
        stc
        for stc in target_columns
        if (
            stc.get('dtype') not in ('serial', 'bigserial')
            and stc.get('sort') and stc['sort'].get('ascending') is not None
        )
    ]

    # If there are any, build ORDER BY section of our select query
    if columns_to_sort_on:
        sort_columns = [
            get_from_clause(
                tables,
                tc,
                source_columns,
                aggregate,
                sort=True,
                variables=variables,
                disable_variables=disable_variables,
                table_numbering_start=table_numbering_start,
            )
            for tc in sorted(columns_to_sort_on, key=lambda stc: stc['sort']['order'])
        ]
        select_query = select_query.order_by(*sort_columns)

    # Build GROUP BY and HAVING sections of our select query.
    if aggregate:
        # GROUP BY
        select_query = select_query.group_by(
            *[
                get_from_clause(
                    tables,
                    tc,
                    source_columns,
                    False,
                    variables=variables,
                    cast=False,
                    disable_variables=disable_variables,
                    table_numbering_start=table_numbering_start,
                )
                for tc in target_columns
                if (
                    tc.get('agg') in ('group', 'group_null')
                    and not tc.get('constant')
                    and not tc.get('dtype') in ('serial', 'bigserial')
                )
            ]
        )

    # Build DISTINCT section of our select query
    if distinct:
        select_query = select_query.distinct(
            *[
                get_from_clause(
                    tables,
                    tc,
                    source_columns,
                    aggregate,
                    variables=variables,
                    disable_variables=disable_variables,
                    table_numbering_start=table_numbering_start,
                )
                for tc in target_columns
                if not tc.get('constant') and
                tc.get('distinct') and not tc.get('dtype') in ('serial', 'bigserial')
            ]
        )

    # HAVING
    if having:
        # CRL 2020 - HAVING clause is now a second select query to apply post-query filters on.
        select_query = apply_output_filter(select_query, having, variables)

    # Build LIMIT and OFFSET sections of our select query
    if use_target_slicer:
        off = limit_target_start
        lim = limit_target_end - off

        select_query = select_query.limit(lim).offset(off)

    return select_query


def get_insert_query(target_table, target_columns, select_query):
    """Returns a sqlalchemy insert query, given a table object, target_columns
    config, and a sqlalchemy select query."""
    return target_table.insert().from_select(
        [tc['target'] for tc in target_columns if tc.get('dtype') not in ('serial', 'bigserial')],
        select_query,
    )


def get_update_query(table, target_columns, wheres, dtype_map, variables=None):
    variables = variables or {}
    update_query = sqlalchemy.update(table)

    combined_wheres = get_combined_wheres(wheres, [table], variables)
    if len(combined_wheres) > 0:
        update_query = update_query.where(sqlalchemy.and_(*combined_wheres))

    # Build values dict
    values = {}
    for tc in target_columns:
        col_name = tc['source']
        dtype = dtype_map.get(col_name, 'text')
        type_ = sqlalchemy_from_dtype(dtype)
        col_val = None
        add_ok = False

        const_val = tc.get('constant')
        expr_val = tc.get('expression')
        null_val = tc.get('nullify', False)

        if null_val:
            col_val = None
            add_ok = True
        else:
            if const_val is not None and len(const_val) > 0:
                col_val = sqlalchemy.literal(
                    apply_variables(const_val, variables),
                    type_=type_,
                )
                add_ok = True

            if expr_val is not None and len(expr_val) > 0:
                col_val = eval_expression(expr_val.strip(), variables, [table])
                add_ok = True

            # Special condition for empty string
            if col_val is None and dtype == 'text':
                # This only applies for text if nullify isn't set to true
                col_val = u''
                add_ok = True

        if add_ok:
            # Don't add any columns that don't have a specified target value
            # This protects users from having additional columns in the list but with no value specified
            values[col_name] = col_val

    update_query = update_query.values(values)

    return update_query


def get_delete_query(table, wheres, variables=None):
    variables = variables or {}

    delete_query = sqlalchemy.delete(table)

    combined_wheres = get_combined_wheres(wheres, [table], variables)
    if len(combined_wheres) > 0:
        delete_query = delete_query.where(sqlalchemy.and_(*combined_wheres))

    return delete_query


def clean_where(w):
    return w.strip().replace('\n', '').replace('\r', '')


def get_combined_wheres(wheres, tables, variables, disable_variables=False, table_numbering_start=1):
    return [
        eval_expression(clean_where(w), variables, tables, disable_variables=disable_variables, table_numbering_start=table_numbering_start)
        for w in wheres if w
    ]


def apply_output_filter(original_query, filter, variables=None):
    original_query = original_query.alias('result')
    where_clause = eval_expression(
        clean_where(filter), variables, [],
        extra_keys={
            'result': original_query.columns
        }
    )
    return sqlalchemy.select(
        original_query.columns
    ).where(
        where_clause
    )


def import_data_query(project_id, target_table_id, source_columns, target_columns, date_format='',
                      trailing_negatives=False, config=None, variables=None):
    """Provides a SQLAlchemy insert query to transfer data from a text import temporary table into the target table.

    Notes:
    Firstly csv is imported into a temporary text table, then it is extracted into the final table via some
    default conversion/string trimming expression. If an expression is provided, it will override the default expression
    The default expression is:
        func.import_col(col, dtype, date_format, trailing_negs)
    which will provide the necessary transformation for each column based on data type

    Args:
        project_id (str): The unique Project Identifier
        target_table_id (str): The target table for the import
        source_columns (list): The list of source columns
        target_columns (list): The list of target columns
        date_format (str, optional): The default date format
        trailing_negatives (bool, optional): Whether to handle trailing negatives in numbers
        config (dict, optional): The step configuration from which filtering/grouping settings can be used
        variables (dict, optional): Variables to use in the query

    Returns:
        sqlalchemy.sql.expression.Insert: The query to import data from the temporary text table to the target table
    """
    metadata = sqlalchemy.MetaData()

    temp_table_id = f'temp_{str(uuid.uuid4())}'
    temp_table_columns = [
        {
            'source': s.get('source', s.get('name', s.get('id'))),
            'dtype': 'text',
        }
        for s in source_columns
    ]
    temp_table_columns.extend([{
        'source': MAGIC_COLUMN_MAPPING[k],
        'dtype': k
    } for k in MAGIC_COLUMN_MAPPING])

    for t in target_columns:
        if t['dtype'] in MAGIC_COLUMN_MAPPING:
            t['source'] = MAGIC_COLUMN_MAPPING[t['dtype']]

    target_meta = [
        {
            'id': t['target'],
            'dtype': t['dtype'],
        } for t in target_columns
    ]

    # Add default expression to target columns
    for tc in target_columns:
        if not tc.get('expression'):
            tc['expression'] = f"""func.import_col(get_column(table, '{tc['source']}'), '{tc['dtype']}', '{date_format}', {trailing_negatives or False})"""

    from_table = get_table_rep_using_id(
        temp_table_id,
        temp_table_columns,
        project_id,
        metadata,
        alias='text_import',
    )

    config = config or {}
    select_query = get_select_query(
        tables=[from_table],
        source_columns=[temp_table_columns],
        target_columns=target_columns,
        wheres=[config.get('source_where')],
        config=config,
        variables=variables,
    )

    # Get the target table rep
    new_table = get_table_rep_using_id(
        target_table_id,
        target_meta,
        project_id,
        metadata,
        column_key='id',
    )

    # Figure out the insert query, based on the select query
    return get_insert_query(new_table, target_columns, select_query)


def allocate(source_query, driver_query, allocate_columns, numerator_columns, denominator_columns, driver_value_column,
             overwrite_cols_for_allocated=True,
             include_source_columns=None,
             unique_cte_index=1):
    """Performs an allocation based on the provided sqlalchemy source and driver data queries

    Args:
        source_query (sqlalchemy.Selectable): Sqlalchemy query for source data
        driver_query (sqlalchemy.Selectable): Sqlalchemy query for driver data
        allocate_columns (list): List of columns to apply a shred to
        numerator_columns (list): List of columns to use as numerator
        denominator_columns (list): List of columns to use as denominator
        driver_value_column (str): Column name for the driver value
        overwrite_cols_for_allocated (bool): Whether to overwrite the source columns with the allocated value, if False, cols suffixed with _allocated are created
        include_source_columns (list, optional): Columns for which we should include a *_source column (for reassignments with multiple allocation steps)
        unique_cte_index (int, optional): Unique index to use in the common table expressions if more than one
            allocation will be done within the same query

    Returns:
        sqlalchemy.Selectable: A Sqlalchemy query representing the allocation
    """
    all_target_columns = [col.name for col in source_query.selected_columns]
    reassignment_columns = [col.name for col in source_query.selected_columns if col.name in numerator_columns]

    all_driver_columns = [col.name for col in driver_query.selected_columns if col.name in numerator_columns + denominator_columns + [driver_value_column]]

    driver_value_columns = [driver_value_column]  # set up for the *possibility* of multiple driver value columns, not sure it makes sense though
    driver_count = len(driver_value_columns)

    include_source_columns = include_source_columns or []

    # Denominator table is SUM of split values with GROUP BY denominator columns
    # Join the Denominator values to the numerator values to get a % split (add an alloc indicator of 1 in a fake column)
    # Outer join the source table to the split % info with a alloc column that is coalesce(driver alloc column, 0).  This produces a 1 if it allocated.
    allocable_col = 'allocable'
    if allocable_col not in all_target_columns:
        source_query = source_query.add_columns(*[sqlalchemy.literal(1).label(allocable_col)])

    def _get_shred_col_name(col):
        return f'shred_{col}' if driver_count > 1 else 'shred'

    def _get_allocated_col_name(col):
        return col if overwrite_cols_for_allocated else f'{col}_allocated'

    cte_source = source_query.cte(f'alloc_source_{unique_cte_index}')
    cte_driver = driver_query.cte(f'alloc_driver_{unique_cte_index}')

    cte_consol_driver = sqlalchemy.select(
        [cte_driver.columns[d] for d in numerator_columns + denominator_columns] +
        [sqlalchemy.func.sum(cte_driver.columns[d]).label(d) for d in driver_value_columns]
    ).where(
        cte_driver.columns[driver_value_column] != 0
    ).group_by(
        *[cte_driver.columns[d] for d in numerator_columns + denominator_columns]
    ).cte(f'consol_driver_{unique_cte_index}')

    cte_denominator = sqlalchemy.select(
        [cte_consol_driver.columns[d] for d in denominator_columns] +
        [sqlalchemy.func.sum(cte_consol_driver.columns[d]).label(d) for d in driver_value_columns]
    ).group_by(
        *[cte_consol_driver.columns[d] for d in denominator_columns]
    ).cte(f'denominator_{unique_cte_index}')

    cte_ratios = sqlalchemy.select(
        [cte_consol_driver.columns[d] for d in denominator_columns + numerator_columns + driver_value_columns] +
        # set ratio to null if the denominator or numerator is zero, this allows pass-through of value to be allocated
        [
            sqlalchemy.func.cast(
                sqlalchemy.func.nullif(
                    sqlalchemy.func.cast(cte_consol_driver.columns[d], sqlalchemy.NUMERIC) / sqlalchemy.func.nullif(
                        cte_denominator.columns[d], 0),
                    0
                ),
                sqlalchemy.NUMERIC
            ).label(_get_shred_col_name(d))
            for d in driver_value_columns
        ]
    ).select_from(
        sqlalchemy.join(
            cte_consol_driver, cte_denominator,
            sqlalchemy.and_(
                *[cte_consol_driver.columns[dn] == cte_denominator.columns[dn] for dn in denominator_columns]
            )
        )
    ).cte(f'ratios_{unique_cte_index}')

    def _is_source_col(col):
        return col not in set(
            reassignment_columns +
            (allocate_columns if overwrite_cols_for_allocated else [])
        )

    allocation_select = sqlalchemy.select(
        [cte_source.columns[tc] for tc in all_target_columns if _is_source_col(tc)] +
        [cte_source.columns[tc].label(f'{tc}_source') for tc in all_target_columns if tc in include_source_columns] +
        [
            sqlalchemy.case(
                [(cte_ratios.columns[_get_shred_col_name(driver_value_columns[0])].isnot(sqlalchemy.null()), cte_ratios.columns[rc])],
                else_=cte_source.columns[rc]
            ).label(rc)
            for rc in reassignment_columns
        ] +
        [cte_ratios.columns[dt] for dt in all_driver_columns if dt not in set(all_target_columns + reassignment_columns)] +
        [
            sqlalchemy.case(
                [(cte_ratios.columns[_get_shred_col_name(driver_value_columns[0])].isnot(sqlalchemy.null()), 1)],
                else_=0
            ).label('alloc_status')
        ] +
        [
            cte_ratios.columns[_get_shred_col_name(d)]
            for d in driver_value_columns
        ] +
        [
            sqlalchemy.case(
                # pass through source value if driver value is null (not found, not allocable, divide by zero)
                [(cte_ratios.columns[_get_shred_col_name(d)].is_(sqlalchemy.null()), cte_source.columns[ac])],
                else_=cte_ratios.columns[_get_shred_col_name(d)] * cte_source.columns[ac]
            ).label(_get_allocated_col_name(ac))
            for ac in allocate_columns
            for d in driver_value_columns
        ]
    ).select_from(
        sqlalchemy.outerjoin(
            cte_source, cte_ratios,
            sqlalchemy.and_(
                *[
                    cte_source.columns[dn] == cte_ratios.columns[dn]
                    for dn in denominator_columns
                ] + [cte_source.columns[allocable_col] == 1]
            )
        )
    ).union_all(
        sqlalchemy.select(
            [cte_source.columns[tc] for tc in all_target_columns if _is_source_col(tc)] +
            [cte_source.columns[tc].label(f'{tc}_source') for tc in all_target_columns if tc in include_source_columns] +
            [cte_source.columns[rc] for rc in reassignment_columns] +
            [cte_ratios.columns[dt] for dt in all_driver_columns if dt not in set(all_target_columns + reassignment_columns)] +
            [sqlalchemy.literal(0, type_=sqlalchemy.Integer).label('alloc_status')] +
            [sqlalchemy.func.cast(sqlalchemy.literal(None), sqlalchemy.Numeric).label(_get_shred_col_name(d)) for d in driver_value_columns] +
            [cte_source.columns[ac].label(_get_allocated_col_name(ac)) for ac in allocate_columns]
        ).where(
            cte_source.columns[allocable_col] == 0
        ).distinct()
    )

    return allocation_select


def apply_rules():
    """"""
    pass
