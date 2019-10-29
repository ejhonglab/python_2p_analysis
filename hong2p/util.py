"""
Common functions for dealing with Thorlabs software output / stimulus metadata /
our databases / movies / CNMF output.
"""

import os
from os import listdir
from os.path import join, split, exists, sep, isdir, normpath, getmtime
import socket
import pickle
import atexit
import signal
import sys
import xml.etree.ElementTree as etree
from types import ModuleType
from datetime import datetime
import warnings
import pprint
import glob
import re

import numpy as np
from numpy.ma import MaskedArray
import pandas as pd
import matplotlib.patches as patches
# is just importing this potentially going to interfere w/ gui?
# put import behind paths that use it?
import matplotlib.pyplot as plt

# Note: many imports were pushed down into the beginnings of the functions that
# use them, to reduce the number of hard dependencies.


recording_cols = [
    'prep_date',
    'fly_num',
    'thorimage_id'
]
trial_only_cols = [
    'comparison',
    'name1',
    'name2',
    'repeat_num'
]
trial_cols = recording_cols + trial_only_cols

date_fmt_str = '%Y-%m-%d'

db_hostname = 'atlas'

# TODO TODO probably just move all stuff that uses db conn into it's own module
# under this package, and then just get the global conn upon that module import
conn = None
def get_db_conn():
    global conn
    global meta
    if conn is not None:
        return conn
    else:
        from sqlalchemy import create_engine, MetaData

        our_hostname = socket.gethostname()
        if our_hostname == db_hostname:
            url = 'postgresql+psycopg2://tracedb:tracedb@localhost:5432/tracedb'
        else:
            url = ('postgresql+psycopg2://tracedb:tracedb@{}' +
                ':5432/tracedb').format(db_hostname)

        conn = create_engine(url)

        # TODO this necessary? was it just for to_sql_with_duplicates or
        # something? why else?
        meta = MetaData()
        meta.reflect(bind=conn)

        return conn


# was too much trouble upgrading my python 3.6 caiman conda env to 3.7
'''
# This is a Python >=3.7 feature only.
def __getattr__(name):
    if name == 'conn':
        return get_db_conn()
    else:
        raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
'''

data_root_name = 'mb_team'

# Module level cache.
_data_root = None
def data_root():
    global _data_root
    if _data_root is None:
        # TODO separate env var for local one? or have that be the default?
        data_root_key = 'HONG_2P_DATA'

        if data_root_key in os.environ:
            data_root = os.environ[data_root_key]
        else:
            nas_prefix_key = 'HONG_NAS'
            if nas_prefix_key in os.environ:
                prefix = os.environ['HONG_NAS']
            else:
                prefix = '/mnt/nas'

            data_root = join(prefix, data_root_name)
        _data_root = data_root
    # TODO err if nothing in data_root, saying which env var to set and how
    return _data_root


# TODO (for both below) support a local and a remote one ([optional] local copy
# for faster repeat analysis)?
# TODO use env var like kc_analysis currently does for prefix after refactoring
# (include mb_team in that part and rename from data_root?)
def raw_data_root():
    return join(data_root(), 'raw_data')


def analysis_output_root():
    return join(data_root(), 'analysis_output')


def stimfile_root():
    return join(data_root(), 'stimulus_data_files')


def _fly_dir(date, fly):
    if not type(date) is str:
        # TODO update to work w/ np.datetime64 too (they don't have strftime)?
        # (+ factor date formatting into its own function that handles the
        # same cases?)
        date = date.strftime(date_fmt_str)

    if not type(fly) is str:
        fly = str(int(fly))

    return join(date, fly)


def raw_fly_dir(date, fly):
    return join(raw_data_root(), _fly_dir(date, fly))


def thorimage_dir(date, fly, thorimage_id):
    return join(raw_fly_dir(date, fly), thorimage_id)


def analysis_fly_dir(date, fly):
    return join(analysis_output_root(), _fly_dir(date, fly))


def matlab_exit_except_hook(exctype, value, traceback):
    if exctype == TypeError:
        args = value.args
        # This message is what MATLAB says in this case.
        if (len(args) == 1 and
            args[0] == 'exit expected at most 1 arguments, got 2'):
            return
    sys.__excepthook__(exctype, value, traceback)


# TODO maybe rename to init_matlab and return nothing, to be more clear that
# other fns here are using it behind the scenes?
evil = None
def matlab_engine():
    """
    Gets an instance of MATLAB engine w/ correct paths for Remy's single plane
    code.
    
    Tries to undo Ctrl-C capturing that MATLAB seems to do.
    """
    import matlab.engine
    global evil

    evil = matlab.engine.start_matlab()
    # TODO TODO this doesn't seem to kill parallel workers... it should
    # (happened in case where there was a memory error. visible in top output.)
    # TODO work inside a fn?
    atexit.register(evil.quit)

    exclude_from_matlab_path = {
        'CaImAn-MATLAB',
        'CaImAn-MATLAB_hong',
        'matlab_helper_functions'
    }
    userpath = evil.userpath()
    for root, dirs, _ in os.walk(userpath, topdown=True):
        dirs[:] = [d for d in dirs if (not d.startswith('.') and
            not d.startswith('@') and not d.startswith('+') and
            d not in exclude_from_matlab_path and 
            d != 'private')]

        evil.addpath(root)

    # Since exiting without letting MATLAB handle it seems to yield a TypeError
    # We will register a global handler that hides that non-useful error
    # message, below.
    signal.signal(signal.SIGINT, sys.exit)
    sys.excepthook = matlab_exit_except_hook

    return evil


# TODO TODO need to acquire a lock to use the matlab instance safely?
# (if i'm sure gui is enforcing only one call at a time anyway, probably
# don't need to worry about it)
def get_matfile_var(matfile, varname, require=True):
    """Returns length-one list with variable contents, or empty list.

    Raises KeyError if require is True and variable not found.
    """
    global evil

    if evil is None:
        matlab_engine()

    try:
        # TODO maybe clear workspace either before or after?
        # or at least clear this specific variable after?
        load_output = evil.load(matfile, varname, nargout=1)
        var = load_output[varname]
        if type(var) is dict:
            return [var]
        return var
    except KeyError:
        # TODO maybe check for var presence some other way than just
        # catching this generic error?
        if require:
            raise
        else:
            return []


# TODO maybe just wrap get_matfile_var?
def load_mat_timing_information(mat_file):
    """Loads and returns timing information from .mat output of Remy's script.

    Raises matlab.engine.MatlabExecutionError
    """
    import matlab.engine
    # TODO this sufficient w/ global above to get access to matlab engine in
    # here?
    global evil

    if evil is None:
        matlab_engine()

    try:
        # TODO probably switch to doing it this way
        '''
        evil.clear(nargout=0)
        load_output = evil.load(mat_file, 'ti', nargout=1)
        ti = load_output['ti']
        '''
        evil.evalc("clear; data = load('{}', 'ti');".format(mat_file))

    except matlab.engine.MatlabExecutionError as e:
        raise
    return evil.eval('data.ti')


# TODO TODO can to_sql with pg_upsert replace this? what extra features did this
# provide?
def to_sql_with_duplicates(new_df, table_name, index=False, verbose=False):
    from sqlalchemy import MetaData, Table

    # TODO TODO document what index means / delete

    # TODO TODO if this fails and time won't be saved on reinsertion, any rows
    # that have been inserted already should be deleted to avoid confusion
    # (mainly, for the case where the program is interrupted while this is
    # running)
    # TODO TODO maybe have some cleaning step that checks everything in database
    # has the correct number of rows? and maybe prompts to delete?

    global conn
    if conn is None:
        conn = get_db_conn()

    # Other columns should be generated by database anyway.
    cols = list(new_df.columns)
    if index:
        cols += list(new_df.index.names)
    table_cols = ', '.join(cols)

    md = MetaData()
    table = Table(table_name, md, autoload_with=conn)
    dtypes = {c.name: c.type for c in table.c}

    if verbose:
        print('SQL column types:')
        pprint.pprint(dtypes)
   
    df_types = new_df.dtypes.to_dict()
    if index:
        df_types.update({n: new_df.index.get_level_values(n).dtype
            for n in new_df.index.names})

    if verbose:
        print('\nOld dataframe column types:')
        pprint.pprint(df_types)

    sqlalchemy2pd_type = {
        'INTEGER()': np.dtype('int32'),
        'SMALLINT()': np.dtype('int16'),
        'REAL()': np.dtype('float32'),
        'DOUBLE_PRECISION(precision=53)': np.dtype('float64'),
        'DATE()': np.dtype('<M8[ns]')
    }
    if verbose:
        print('\nSQL types to cast:')
        pprint.pprint(sqlalchemy2pd_type)

    new_df_types = {n: sqlalchemy2pd_type[repr(t)] for n, t in dtypes.items()
        if repr(t) in sqlalchemy2pd_type}

    if verbose:
        print('\nNew dataframe column types:')
        pprint.pprint(new_df_types)

    # TODO how to get around converting things to int if they have NaN.
    # possible to not convert?
    new_column_types = dict()
    new_index_types = dict()
    for k, t in new_df_types.items():
        if k in new_df.columns and not new_df[k].isnull().any():
            new_column_types[k] = t

        # TODO or is it always true that index level can't be NaN anyway?
        elif (k in new_df.index.names and
            not new_df.index.get_level_values(k).isnull().any()):

            new_index_types[k] = t

        # TODO print types being skipped b/c nan?

    new_df = new_df.astype(new_column_types, copy=False)
    if index:
        # TODO need to handle case where conversion dict is empty
        # (seems to fail?)
        #pprint.pprint(new_index_types)

        # MultiIndex astype method seems to not work the same way?
        new_df.index = pd.MultiIndex.from_frame(
            new_df.index.to_frame().astype(new_index_types, copy=False))

    # TODO print the type of any sql types not convertible?
    # TODO assert all dtypes can be converted w/ this dict?

    if index:
        print('writing to temporary table temp_{}...'.format(table_name))

    # TODO figure out how to profile
    new_df.to_sql('temp_' + table_name, conn, if_exists='replace', index=index,
        dtype=dtypes)

    # TODO change to just get column names?
    query = '''
    SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS data_type
    FROM   pg_index i
    JOIN   pg_attribute a ON a.attrelid = i.indrelid
        AND a.attnum = ANY(i.indkey)
    WHERE  i.indrelid = '{}'::regclass
    AND    i.indisprimary;
    '''.format(table_name)
    result = conn.execute(query)
    pk_cols = ', '.join([n for n, _ in result])

    # TODO TODO TODO modify so on conflict the new row replaces the old one!
    # (for updates to analysis, if exact code version w/ uncommited changes and
    # everything is not going to be part of primary key...)
    # (want updates to non-PK rows)

    # TODO TODO should i just delete rows w/ our combination(s) of pk_cols?
    # (rather than other upsert strategies)
    # TODO (flag to) check deletion was successful
    # TODO factor deletion into another fn (?) and expose separately in gui


    # TODO prefix w/ ANALYZE EXAMINE and look at results
    query = ('INSERT INTO {0} ({1}) SELECT {1} FROM temp_{0} ' +
        'ON CONFLICT ({2}) DO NOTHING').format(table_name, table_cols, pk_cols)
    # TODO maybe a merge is better for this kind of upsert, in postgres?
    if index:
        # TODO need to stdout flush or something?
        print('inserting into {} from temporary table... '.format(table_name),
            end='')

    # TODO let this happen async in the background? (don't need result)
    conn.execute(query)

    # TODO flag to read back and check insertion stored correct data?

    if index:
        print('done')

    # TODO drop staging table


def pg_upsert(table, conn, keys, data_iter):
    from sqlalchemy.dialects import postgresql
    # https://github.com/pandas-dev/pandas/issues/14553
    for row in data_iter:
        row_dict = dict(zip(keys, row))
        sqlalchemy_table = meta.tables[table.name]
        stmt = postgresql.insert(sqlalchemy_table).values(**row_dict)
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=table.index,
            set_=row_dict)
        conn.execute(upsert_stmt)


def df_to_odorset_name(df):
    """Returns name for set of odors in DataFrame.

    Looks at odors in original_name1 column. Name used to lookup desired
    plotting order for the odors in the set.
    """
    if 'ethyl butyrate' in df.original_name1.unique():
        odor_set = 'kiwi'
    else:
        odor_set = 'control'
    # TODO probably just find single odor that satisfies is_mix and derive from
    # that, for more generality
    return odor_set


# TODO TODO use this for complex mixture experiment output plotting in gui
# (downstream of plot order flags currently not implemented)
odor_set2order = {
    'kiwi': [
        'pfo',
        'ethyl butyrate',
        'ethyl acetate',
        'isoamyl acetate',
        'isoamyl alcohol',
        'ethanol',
        'd3 kiwi',
        'kiwi approx.'
    ],
    'control': [
        'pfo',
        '1-octen-3-ol',
        'furfural',
        'valeric acid',
        'methyl salicylate',
        '2-heptanone',
        # Only one of these will actually be present, they just take the same
        # place in the order.
        'control mix 1'
        'control mix 2'
    ]
}
def df_to_odor_order(df):
    # TODO might need to use name1 if original_name1 not there...
    # (for gui case)
    odor_set = df_to_odorset_name(df)
    return [o for o in odor_set2order[odor_set] if o in
        df.original_name1.unique()]


def old_fmt_thorimage_num(x):
    if pd.isnull(x) or not (x[0] == '_' and len(x) == 4):
        return np.nan
    try:
        n = int(x[1:])
        return n
    except ValueError:
        return np.nan


def new_fmt_thorimage_num(x):
    parts = x.split('_')
    if len(parts) == 1:
        return 0
    else:
        return int(x[-1])


def thorsync_num(x):
    prefix = 'SyncData'
    return int(x[len(prefix):])


_mb_team_gsheet = None
def mb_team_gsheet(use_cache=False, show_inferred_paths=False,
    natural_odors_only=False):
    '''Returns a pandas.DataFrame with data on flies and MB team recordings.
    '''
    global _mb_team_gsheet
    if _mb_team_gsheet is not None:
        return _mb_team_gsheet

    gsheet_cache_file = '.gsheet_cache.p'
    if use_cache and exists(gsheet_cache_file):
        print('Loading MB team sheet data from cache at {}'.format(
            gsheet_cache_file))

        with open(gsheet_cache_file, 'rb') as f:
            sheets = pickle.load(f)

    else:
        # TODO TODO maybe env var pointing to this? or w/ link itself?
        # TODO maybe just get relative path from __file__ w/ /.. or something?
        pkg_data_dir = split(split(__file__)[0])[0]
        with open(join(pkg_data_dir, 'mb_team_sheet_link.txt'), 'r') as f:
            gsheet_link = \
                f.readline().split('/edit')[0] + '/export?format=csv&gid='

        # If you want to add more sheets, when you select the new sheet in your
        # browser, the GID will be at the end of the URL in the address bar.
        sheet_gids = {
            'fly_preps': '269082112',
            'recordings': '0',
            'daily_settings': '229338960'
        }

        sheets = dict()
        for df_name, gid in sheet_gids.items():
            df = pd.read_csv(gsheet_link + gid)

            # TODO convert any other dtypes?
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])

            df.drop(columns=[c for c in df.columns
                if c.startswith('Unnamed: ')], inplace=True)

            if 'fly_num' in df.columns:
                last_with_fly_num = df.fly_num.notnull()[::-1].idxmax()
                df.drop(df.iloc[(last_with_fly_num + 1):].index, inplace=True)

            sheets[df_name] = df

        boolean_columns = {
            'attempt_analysis',
            'raw_data_discarded',
            'raw_data_lost'
        }
        na_cols = list(set(sheets['recordings'].columns) - boolean_columns)
        sheets['recordings'].dropna(how='all', subset=na_cols, inplace=True)

        with open(gsheet_cache_file, 'wb') as f:
            pickle.dump(sheets, f) 

    # TODO maybe make df some merge of the three sheets?
    df = sheets['recordings']

    # TODO TODO maybe flag to disable path inference / rethink how it should
    # interact w/ timestamp based correspondence between thorsync/image and
    # mapping that to the recordings in the gsheet
    # TODO should inference that reads the metadata happen in this fn?
    # maybe yes, but still factor it out and just call here?

    # TODO maybe start by not filling in fully-empty groups / flagging
    # them for later -> preferring to infer those from local files ->
    # then inferring fully-empty groups from default numbering as before

    keys = ['date', 'fly_num']
    # These should happen before rows start being dropped, because the dropped
    # rows might have the information needed to ffill.
    # This should NOT ffill a fly_past a change in date.

    # Assuming that if date changes, even if fly_nums keep going up, that was
    # intentional.
    df.date = df.date.fillna(method='ffill')
    df.fly_num = df.groupby('date')['fly_num'].apply(
        lambda x: x.ffill().bfill()
    )
    # This will only apply to groups (dates) where there are ONLY missing
    # fly_nums, given filling logic above.
    df.dropna(subset=['fly_num'], inplace=True)
    assert not df.date.isnull().any()

    df['stimulus_data_file'] = df['stimulus_data_file'].fillna(method='ffill')

    df.raw_data_discarded = df.raw_data_discarded.fillna(False)
    # TODO say when this happens?
    df.drop(df[df.raw_data_discarded].index, inplace=True)

    # TODO TODO warn if 'attempt_analysis' and either discard / lost is checked

    # Not sure where there were any NaN here anyway...
    df.raw_data_lost = df.raw_data_lost.fillna(False)
    df.drop(df[df.raw_data_lost].index, inplace=True)

    # TODO as per note below, any thorimage/thorsync dirs entered in spreadsheet
    # should probably cause warning/err if either of above rejection reason
    # is checked

    # This happens after data is dropped for the above two reasons, because
    # generally those mistakes do not consume any of our sequential filenames.
    # They should not have files associated with them, and the Google sheet
    # information on them is just for tracking problems / efficiency.
    df['recording_num'] = df.groupby(keys).cumcount() + 1

    if show_inferred_paths:
        missing_thorimage = pd.isnull(df.thorimage_dir)
        missing_thorsync = pd.isnull(df.thorsync_dir)

    prep_checking = 'n/a (prep checking)'
    my_project = 'natural_odors'

    check_and_set = []
    for gn, gdf in df.groupby(keys):
        if not (gdf.project == my_project).any():
            continue

        if gdf[['thorimage_dir','thorsync_dir']].isnull().all(axis=None):
            fly_dir = raw_fly_dir(*gn)
            if not exists(fly_dir):
                continue

            #print('\n' + fly_dir)
            try:
                image_and_sync_pairs = pair_thor_subdirs(fly_dir)
                #print('pairs:')
                #pprint.pprint(image_and_sync_pairs)
            except ValueError as e:
                gn_str = format_keys(*gn)
                print(f'For {gn_str}:')
                print('could not pair thor dirs automatically!')
                print(f'({e})\n')
                continue

            # could maybe try to sort things into "prep checking" / real
            # experiment based on time length or something (and maybe try
            # to fall back to just pairing w/ real experiments? and extending
            # condition below to # real experiments in gdf)
            nm = len(image_and_sync_pairs)
            ng = len(gdf)
            if nm < ng:
                #print('more rows for (date, fly) pair than matched outputs'
                #    f' ({nm} < {ng})')
                continue

            all_group_in_old_dir_fmt = True
            group_tids = []
            group_tsds = []
            for tid, tsd in image_and_sync_pairs:
                tid = split(tid)[-1]
                if pd.isnull(old_fmt_thorimage_num(tid)):
                    #print(f'{tid} not in old format')
                    all_group_in_old_dir_fmt = False

                group_tids.append(tid)
                group_tsds.append(split(tsd)[-1])

            # Not immediately setting df in this case, so that I can check
            # these results against the old way of doing things.
            if all_group_in_old_dir_fmt:
                #print('all in old dir format')
                check_and_set.append((gn, gdf.index, group_tids, group_tsds))
            else:
                #print('filling in b/c not (all) in old dir format')
                # TODO is it ok to modify df used to create groupby while
                # iterating over groupby?
                df.loc[gdf.index, 'thorimage_dir'] = group_tids
                df.loc[gdf.index, 'thorsync_dir'] = group_tsds

    df.drop(df[df.project != 'natural_odors'].index, inplace=True)

    # TODO TODO implement option to (at least) also keep prep checking that
    # preceded natural_odors (or maybe just that was on the same day)
    # (so that i can get all that ethyl acetate data for use as a reference
    # odor)

    # TODO display stuff inferred from files separately from stuff inferred
    # from combination of gsheet info and convention

    df['thorimage_num'] = df.thorimage_dir.apply(old_fmt_thorimage_num)
    df['numbering_consistent'] = \
        pd.isnull(df.thorimage_num) | (df.thorimage_num == df.recording_num)

    # TODO unit test this
    # TODO TODO check that, if there are mismatches here, that they *never*
    # happen when recording num will be used for inference in rows in the group
    # *after* the mismatch
    gkeys = keys + ['thorimage_dir','thorsync_dir','thorimage_num',
                    'recording_num','numbering_consistent']
    for name, group_df in df.groupby(keys):
        # TODO maybe refactor above so case 3 collapses into case 1?
        '''
        Case 1: all consistent
        Case 2: not all consistent, but all thorimage_dir filled in
        Case 3: not all consistent, but just because thorimage_dir was null
        '''
        #print(group_df[gkeys])

        # TODO check that first_mismatch based approach includes this case
        #if pd.notnull(group_df.thorimage_dir).all():
        #    continue

        mismatches = np.argwhere(~ group_df.numbering_consistent)
        if len(mismatches) == 0:
            continue

        first_mismatch_idx = mismatches[0][0]
        #print('first_mismatch:\n', group_df[gkeys].iloc[first_mismatch_idx])

        # TODO test case where the first mismatch is last
        following_thorimage_dirs = \
            group_df.thorimage_dir.iloc[first_mismatch_idx:]
        #print('checking these are not null:\n', following_thorimage_dirs)
        assert pd.notnull(following_thorimage_dirs).all()

    df.thorsync_dir.fillna(df.thorimage_num.apply(lambda x:
        np.nan if pd.isnull(x) else 'SyncData{:03d}'.format(int(x))),
        inplace=True
    )

    # Leaving recording_num because it might be prettier to use that for
    # IDs in figure than whatever Thor output directory naming convention.
    df.drop(columns=['thorimage_num','numbering_consistent'], inplace=True)

    # TODO TODO check for conditions in which we might need to renumber
    # recording num? (dupes / any entered numbers along the way that are
    # inconsistent w/ recording_num results)
    # TODO update to handle case where thorimage dir does not start w/
    # _ and is not just 3 digits after that?
    # (see what format other stuff from day is?)
    df.thorimage_dir.fillna(df.recording_num.apply(lambda x:
        np.nan if pd.isnull(x) else '_{:03d}'.format(int(x))), inplace=True
    )
    df.thorsync_dir.fillna(df.recording_num.apply(lambda x:
        np.nan if pd.isnull(x) else 'SyncData{:03d}'.format(int(x))),
        inplace=True
    )

    for gn, gidx, gtids, gtsds in check_and_set:
        # Since some stuff may have been dropped (prep checking stuff, etc).
        still_in_idx = gidx.isin(df.index)
        # No group w/ files on NAS should have been dropped completely.
        assert still_in_idx.sum() > 0, f'group {gn} dropped completely'

        gidx = gidx[still_in_idx]
        gtids = np.array(gtids)[still_in_idx]
        gtsds = np.array(gtsds)[still_in_idx]

        from_gsheet = df.loc[gidx, ['thorimage_dir', 'thorsync_dir']]
        from_thor = [gtids, gtsds]
        consistent = (from_gsheet == from_thor).all(axis=None)
        if not consistent:
            print('Inconsistency between path infererence methods!')
            print(dict(zip(keys, gn)))
            print('Derived from Google sheet:')
            print(from_gsheet.T.to_string(header=False))
            print('From matching Thor output files:')
            print(pd.DataFrame(dict(zip(from_gsheet.columns, from_thor))
                ).T.to_string(header=False))
            print('')
            raise AssertionError('inconsistent rankings w/ old format')

    if show_inferred_paths:
        cols = keys + ['thorimage_dir','thorsync_dir']
        print('Inferred ThorImage directories:')
        print(df.loc[missing_thorimage, cols])
        print('\nInferred ThorSync directories:')
        print(df.loc[missing_thorsync, cols])
        print('')

    duped_thorimage = df.duplicated(subset=keys + ['thorimage_dir'], keep=False)
    duped_thorsync = df.duplicated(subset=keys + ['thorsync_dir'], keep=False)
    try:
        assert not duped_thorimage.any()
        assert not duped_thorsync.any()
    except AssertionError:
        print('Duplicated ThorImage directories after path inference:')
        print(df[duped_thorimage])
        print('\nDuplicated ThorSync directories after path inference:')
        print(df[duped_thorsync])
        raise

    flies = sheets['fly_preps']
    flies['date'] = flies['date'].fillna(method='ffill')
    flies.dropna(subset=['date','fly_num'], inplace=True)

    # TODO maybe flag to not update database? or just don't?
    # TODO groups all inserts into transactions across tables, and as few as
    # possible (i.e. only do this later)?
    to_sql_with_duplicates(flies.rename(
        columns={'date': 'prep_date'}), 'flies'
    )

    _mb_team_gsheet = df

    # TODO handle case where database is empty but gsheet cache still exists
    # (all inserts will probably fail, for lack of being able to reference fly
    # table)
    return df


def merge_gsheet(df, *args, use_cache=False):
    """
    df must have a column named either 'recording_from' or 'started_at'

    gsheet rows get this information by finding the right ThorImage
    Experiment.xml files on the NAS and loading them for this timestamp.
    """
    if len(args) == 0:
        gsdf = mb_team_gsheet(use_cache=use_cache)
    elif len(args) == 1:
        # TODO maybe copy in this case?
        gsdf = args[0]
    else:
        raise ValueError('incorrect number of arguments')

    if 'recording_from' in df.columns:
        # TODO maybe just merge_recordings w/ df in advance in this case?
        df = df.rename(columns={'recording_from': 'started_at'})
    elif 'started_at' not in df.columns:
        raise ValueError("df needs 'recording_from'/'started_at' in columns")

    gsdf['recording_from'] = pd.NaT
    for i, row in gsdf.iterrows():
        date_dir = row.date.strftime(date_fmt_str)
        fly_num = str(int(row.fly_num))
        thorimage_dir = join(raw_data_root(),
            date_dir, fly_num, row.thorimage_dir)
        thorimage_xml_path = join(thorimage_dir, 'Experiment.xml')

        try:
            xml_root = _xmlroot(thorimage_xml_path)
        except FileNotFoundError as e:
            continue

        gsdf.loc[i, 'recording_from'] = get_thorimage_time_xml(xml_root)

    # TODO fail if stuff marked attempt_analysis has missing xml files?
    # or if nothing was found?

    gsdf = gsdf.rename(columns={'date': 'prep_date'})

    return merge_recordings(gsdf, df, verbose=False)


def merge_odors(df, *args):
    global conn
    if conn is None:
        conn = get_db_conn()

    if len(args) == 0:
        odors = pd.read_sql('odors', conn)
    elif len(args) == 1:
        odors = args[0]
    else:
        raise ValueError('incorrect number of arguments')

    print('merging with odors table...', end='', flush=True)
    # TODO way to do w/o resetting index? merge failing to find odor1 or just
    # drop?
    # TODO TODO TODO do i want drop=True? (it means cols in index won't be
    # inserted into dataframe...) check use of merge_odors and change to
    # drop=False (default) if it won't break anything
    df = df.reset_index(drop=True)

    df = pd.merge(df, odors, left_on='odor1', right_on='odor_id',
                  suffixes=(False, False))

    df.drop(columns=['odor_id','odor1'], inplace=True)
    df.rename(columns={'name': 'name1',
        'log10_conc_vv': 'log10_conc_vv1'}, inplace=True)

    df = pd.merge(df, odors, left_on='odor2', right_on='odor_id',
                  suffixes=(False, False))

    df.drop(columns=['odor_id','odor2'], inplace=True)
    df.rename(columns={'name': 'name2',
        'log10_conc_vv': 'log10_conc_vv2'}, inplace=True)

    print(' done')

    # TODO refactor merge fns to share some stuff? (progress, length checking,
    # arg unpacking, etc)?
    return df


def merge_recordings(df, *args, verbose=True):
    global conn
    if conn is None:
        conn = get_db_conn()

    if len(args) == 0:
        recordings = pd.read_sql('recordings', conn)
    elif len(args) == 1:
        recordings = args[0]
    else:
        raise ValueError('incorrect number of arguments')

    print('merging with recordings table...', end='', flush=True)
    len_before = len(df)
    # TODO TODO TODO do i want drop=True? (it means cols in index won't be
    # inserted into dataframe...) check use of this fn and change to
    # drop=False (default) if it won't break anything
    df = df.reset_index(drop=True)

    df = pd.merge(df, recordings, how='left', left_on='recording_from',
        right_on='started_at', suffixes=(False, False))

    df.drop(columns=['started_at'], inplace=True)

    # TODO TODO see notes in kc_analysis about sub-recordings and how that
    # will now break this in the recordings table
    # (multiple dirs -> one start time)
    df['thorimage_id'] = df.thorimage_path.apply(lambda x: split(x)[-1])
    assert len_before == len(df), 'merging changed input df length'
    print(' done')
    return df


def arraylike_cols(df):
    """Returns a list of columns that have only lists or arrays as elements.
    """
    df = df.select_dtypes(include='object')
    return df.columns[df.applymap(lambda o:
        type(o) is list or isinstance(o, np.ndarray)).all()]


# TODO use in other places that duplicate this functionality
# (like in natural_odors/kc_analysis ?)
def expand_array_cols(df):
    """Expands any list/array entries, with new rows for each entry.

    For any columns in `df` that have all list/array elements (at each row),
    the column in `out_df` will have the type of single elements from those
    arrays.

    The length of `out_df` will be the length of the input `df`, multiplied by
    the length (should be common in each input row) of each set of list/array
    elements.

    Other columns have their values duplicated, to match the lengths of the
    expanded array values.

    Args:
    `df` (pd.DataFrame)

    Returns:
    `out_df` (pd.DataFrame)
    """
    if len(df.index.names) > 1 or df.index.names[0] is not None:
        raise NotImplementedError('numpy repeating may not handle index. '
            'reset_index first.')

    # Will be ['raw_f', 'df_over_f', 'from_onset'] in the main way I'm using
    # this function.
    array_cols = arraylike_cols(df)

    if len(array_cols) == 0:
        raise ValueError('df did not appear to have any columns with all '
            'arraylike elements')

    orig_dtypes = df.dtypes.to_dict()
    for ac in array_cols:
        df[ac] = df[ac].apply(lambda x: np.array(x))
        assert len(df[ac]) > 0 and len(df[ac][0]) > 0
        orig_dtypes[ac] = df[ac][0][0].dtype

    non_array_cols = df.columns.difference(array_cols)

    # TODO true vectorized way to do this?
    # is str.len (on either rows/columns) faster (+equiv)?
    array_lengths = df[array_cols].applymap(len)
    c0 = array_lengths[array_cols[0]]
    for c in array_cols[1:]:
        assert np.array_equal(c0, array_lengths[c])
    array_lengths = c0

    # TODO more idiomatic / faster way to do what this loop is doing?
    n_non_array_cols = len(non_array_cols)
    expanded_rows_list = []
    for row, n_repeats in zip(df[non_array_cols].values, array_lengths):
        # could try subok=True if want to use pandas obj as input rather than
        # stuff from .values?
        expanded_rows = np.broadcast_to(row, (n_repeats, n_non_array_cols))
        expanded_rows_list.append(expanded_rows)
    nac_data = np.concatenate(expanded_rows_list, axis=0)

    ac_data = df[array_cols].apply(np.concatenate)
    assert nac_data.shape[0] == ac_data.shape[0]
    data = np.concatenate((nac_data, ac_data), axis=1)
    assert data.shape[1] == df.shape[1]

    new_cols = list(non_array_cols) + list(array_cols)
    # TODO copy=False is fine here, right? measure the time difference?
    out_df = pd.DataFrame(columns=new_cols, data=data).astype(orig_dtypes,
        copy=False)

    return out_df


def diff_dataframes(df1, df2):
    """Returns a DataFrame summarizing input differences.
    """
    # TODO do i want df1 and df2 to be allowed to be series?
    # (is that what they are now? need to modify anything?)
    assert (df1.columns == df2.columns).all(), \
        "DataFrame column names are different"
    if any(df1.dtypes != df2.dtypes):
        "Data Types are different, trying to convert"
        df2 = df2.astype(df1.dtypes)
    # TODO is this really necessary? not an empty df in this case anyway?
    if df1.equals(df2):
        return None
    else:
        # TODO unit test w/ descrepencies in each of the cases.
        # TODO also test w/ nan in list / nan in float column (one / both nan)
        floats1 = df1.select_dtypes(include='float')
        floats2 = df2.select_dtypes(include='float')
        assert set(floats1.columns) == set(floats2.columns)
        diff_mask_floats = ~pd.DataFrame(
            columns=floats1.columns,
            index=df1.index,
            # TODO TODO does this already deal w/ nan correctly?
            # otherwise, this part needs to handle possibility of nan
            # (it does not. need to handle.)
            data=np.isclose(floats1, floats2)
        )
        diff_mask_floats = (diff_mask_floats &
            ~(floats1.isnull() & floats2.isnull()))

        # Just assuming, for now, that array-like cols are same across two dfs.
        arr_cols = arraylike_cols(df1)
        # Also assuming, for now, that no elements of these lists / arrays will
        # be nan (which is currently true).
        diff_mask_arr = ~pd.DataFrame(
            columns=arr_cols,
            index=df1.index,
            data=np.vectorize(np.allclose)(df1[arr_cols], df2[arr_cols])
        )

        other_cols = set(df1.columns) - set(floats1.columns) - set(arr_cols)
        other_diff_mask = df1[other_cols] != df2[other_cols]

        diff_mask = pd.concat([
            diff_mask_floats,
            diff_mask_arr,
            other_diff_mask], axis=1)

        if diff_mask.sum().sum() == 0:
            return None

        ne_stacked = diff_mask.stack()
        changed = ne_stacked[ne_stacked]
        # TODO are these what i want? prob change id (basically just to index?)?
        # TODO get id from index name of input dfs? and assert only one index
        # (assuming this wouldn't work w/ multiindex w/o modification)?
        changed.index.names = ['id', 'col']
        difference_locations = np.where(diff_mask)
        changed_from = df1.values[difference_locations]
        changed_to = df2.values[difference_locations]
        return pd.DataFrame({'from': changed_from, 'to': changed_to},
                            index=changed.index)


def first_group(df, group_cols):
    """Returns key tuple and df of first group, grouping df on group_cols.

    Just for ease of interactively testing out functions on DataFrames of a
    groupby.
    """
    gb = df.groupby(group_cols)
    first_group_tuple = list(gb.groups.keys())[0]
    gdf = gb.get_group(first_group_tuple)
    return first_group_tuple, gdf


def git_hash(repo_file):
    """Takes any file in a git directory and returns current hash.
    """
    import git
    repo = git.Repo(repo_file, search_parent_directories=True)
    current_hash = repo.head.object.hexsha
    return current_hash


# TODO TODO maybe check that remote seems to be valid, and fail if not.
# don't want to assume we have an online (backed up) record of git repo when we
# don't...
def version_info(module_or_path, used_for=''):
    """Takes module or string path to file in Git repo to a dict with version
    information (with keys and values the database will accept).
    """
    import git
    import pkg_resources

    if isinstance(module_or_path, ModuleType):
        module = module_or_path
        pkg_path = module.__file__
        name = module.__name__
    else:
        if type(module_or_path) != str:
            raise ValueError('must path either a Python module or str path')
        pkg_path = module_or_path
        module = None

    try:
        repo = git.Repo(pkg_path, search_parent_directories=True)
        name = split(repo.working_tree_dir)[-1]
        remote_urls = list(repo.remotes.origin.urls)
        assert len(remote_urls) == 1
        remote_url = remote_urls[0]

        current_hash = repo.head.object.hexsha

        index = repo.index
        diff = index.diff(None, create_patch=True)
        changes = ''
        for d in diff:
            changes += str(d)

        return {
            'name': name,
            'used_for': used_for,
            'git_remote': remote_url,
            'git_hash': current_hash,
            'git_uncommitted_changes': changes
        }

    except git.exc.InvalidGitRepositoryError:
        if module is None:
            # TODO try to find module from str
            raise NotImplementedError(
                'pass module for non-source installations')

        # There may be circumstances in which module name isn't the right name
        # to use here, but assuming we won't encounter that for now.
        version = pkg_resources.get_distribution(module.__name__).version

        return {'name': name, 'used_for': used_for, 'version': version}


def upload_code_info(code_versions):
    """Returns a list of integer IDs for inserted code version rows.

    code_versions should be a list of dicts, each dict representing a row in the
    corresponding table.
    """
    global conn
    if conn is None:
        conn = get_db_conn()

    if len(code_versions) == 0:
        raise ValueError('code versions can not be empty')

    code_versions_df = pd.DataFrame(code_versions)
    # TODO delete try/except
    try:
        code_versions_df.to_sql('code_versions', conn, if_exists='append',
            index=False)
    except:
        print(code_versions_df)
        import ipdb; ipdb.set_trace()

    # TODO maybe only read most recent few / restrict to some other key if i
    # make one?
    db_code_versions = pd.read_sql('code_versions', conn)

    our_version_cols = code_versions_df.columns
    version_ids = list()
    for _, row in code_versions_df.iterrows():
        # This should take the *first* row that is equal.
        idx = (db_code_versions[code_versions_df.columns] == row).all(
            axis=1).idxmax()
        version_id = db_code_versions['version_id'].iat[idx]
        assert version_id not in version_ids
        version_ids.append(version_id)

    return version_ids


def upload_analysis_info(*args) -> None:
    """
    Requires that corresponding row in analysis_runs table already exists,
    if only two args are passed.
    """
    global conn
    if conn is None:
        conn = get_db_conn()

    have_ids = False
    if len(args) == 2:
        analysis_started_at, code_versions = args
    elif len(args) == 3:
        recording_started_at, analysis_started_at, code_versions = args

        if len(code_versions) == 0:
            raise ValueError('code_versions can not be empty')

        if type(code_versions) == list and np.issubdtype(
            type(code_versions[0]), np.integer):

            version_ids = code_versions
            have_ids = True

        pd.DataFrame({
            'run_at': [analysis_started_at],
            'recording_from': recording_started_at
        }).set_index('run_at').to_sql('analysis_runs', conn,
            if_exists='append', method=pg_upsert)

    else:
        raise ValueError('incorrect number of arguments')

    if not have_ids:
        version_ids = upload_code_info(code_versions)

    analysis_code = pd.DataFrame({
        'run_at': analysis_started_at,
        'version_id': version_ids
    })
    to_sql_with_duplicates(analysis_code, 'analysis_code')


def motion_corrected_tiff_filename(date, fly_num, thorimage_id):
    """Takes vars identifying recording to the name of a motion corrected TIFF
    for it. Non-rigid preferred over rigid. Relies on naming convention.
    """
    tif_dir = join(analysis_fly_dir(date, fly_num), 'tif_stacks')
    nr_tif = join(tif_dir, '{}_nr.tif'.format(thorimage_id))
    rig_tif = join(tif_dir, '{}_rig.tif'.format(thorimage_id))
    tif = None
    if exists(nr_tif):
        tif = nr_tif
    elif exists(rig_tif):
        tif = rig_tif

    if tif is None:
        raise IOError('No motion corrected TIFs found in {}'.format(tif_dir))

    return tif


# TODO use this in other places that normalize to thorimage_ids
def tiff_thorimage_id(tiff_filename):
    """
    Takes a path to a TIFF and returns ID to identify recording within
    (date, fly). Relies on naming convention.
    """
    # Behavior of os.path.split makes this work even if tiff_filename does not
    # have any directories in it.
    return '_'.join(split(tiff_filename[:-len('.tif')])[1].split('_')[:2])


# TODO don't expose this if i can refactor other stuff to not use it
# otherwise use this rather than their own separate definitions
# (like in populate_db, etc)
rel_to_cnmf_mat = 'cnmf'
def matfile(date, fly_num, thorimage_id):
    """Returns filename of Remy's metadata [+ CNMF output] .mat file.
    """
    return join(analysis_fly_dir(date, fly_num), rel_to_cnmf_mat,
        thorimage_id + '_cnmf.mat'
    )


def tiff_matfile(tif):
    """Returns filename of Remy's metadata [+ CNMF output] .mat file.
    """
    keys = tiff_filename2keys(tif)
    return matfile(*keys)


def metadata_filename(date, fly_num, thorimage_id):
    """Returns filename of YAML for extra metadata.
    """
    return join(raw_fly_dir(date, fly_num), thorimage_id + '_metadata.yaml')


# TODO maybe something to indicate various warnings
# (like mb team not being able to pair things) should be suppressed?
def metadata(date, fly_num, thorimage_id):
    """Returns metadata from YAML, with defaults added.
    """
    import yaml

    metadata_file = metadata_filename(date, fly_num, thorimage_id)

    # TODO another var specifying number of frames that has *already* been
    # cropped out of raw tiff (start/end), to resolve any descrepencies wrt 
    # thorsync data
    metadata = {
        'drop_first_n_frames': 0
    }
    if exists(metadata_file):
        # TODO TODO TODO also load single odors (or maybe other trial
        # structures) from stuff like this, so analysis does not need my own
        # pickle based stim format
        with open(metadata_file, 'r') as mdf:
            yaml_metadata = yaml.load(mdf)

        for k in metadata.keys():
            if k in yaml_metadata:
                metadata[k] = yaml_metadata[k]

    return metadata


def tiff_filename2keys(tiff_filename):
    """Takes TIFF filename to pd.Series w/ 'date','fly_num','thorimage_id' keys.

    TIFF must be placed and named according to convention, because the
    date and fly_num are taken from names of some of the containing directories.
    """
    parts = tiff_filename.split(sep)[-4:]
    date = pd.Timestamp(datetime.strptime(parts[0], date_fmt_str))
    fly_num = int(parts[1])
    # parts[2] will be 'tif_stacks'
    thorimage_id = tiff_thorimage_id(tiff_filename)
    return pd.Series({
        'date': date, 'fly_num': fly_num, 'thorimage_id': thorimage_id
    })


def list_motion_corrected_tifs(include_rigid=False, attempt_analysis_only=True):
    """List motion corrected TIFFs in conventional directory structure on NAS.
    """
    motion_corrected_tifs = []
    df = mb_team_gsheet()
    for full_date_dir in sorted(glob.glob(join(analysis_output_root(), '**'))):
        for full_fly_dir in sorted(glob.glob(join(full_date_dir, '**'))):
            date_dir = split(full_date_dir)[-1]
            try:
                fly_num = int(split(full_fly_dir)[-1])

                fly_used = df.loc[df.attempt_analysis &
                    (df.date == date_dir) & (df.fly_num == fly_num)]

                used_thorimage_dirs = set(fly_used.thorimage_dir)

                tif_dir = join(full_fly_dir, 'tif_stacks')
                if exists(tif_dir):
                    tif_glob = '*.tif' if include_rigid else '*_nr.tif'
                    fly_tifs = sorted(glob.glob(join(tif_dir, tif_glob)))

                    used_tifs = [x for x in fly_tifs if '_'.join(
                        split(x)[-1].split('_')[:-1]) in used_thorimage_dirs]

                    motion_corrected_tifs += used_tifs

            except ValueError:
                continue

    return motion_corrected_tifs


def list_segmentations(tif_path):
    """Returns a DataFrame of segmentation_runs for given motion corrected TIFF.
    """
    global conn
    if conn is None:
        conn = get_db_conn()

    # TODO could maybe turn these two queries into one (WITH semantics?)
    # TODO TODO should maybe trim all prefixes from input_filename before
    # uploading? unless i want to just figure out path from other variables each
    # time and use that to match (if NAS_PREFIX is diff, there will be no match)
    prefix = analysis_output_root()
    if tif_path.startswith(prefix):
        tif_path = tif_path[len(prefix):]

    # TODO test this strpos stuff is equivalent to where input_filename = x
    # in case where prefixes are the same
    analysis_runs = pd.read_sql_query('SELECT * FROM analysis_runs WHERE ' +
        "strpos(input_filename, '{}') > 0".format(tif_path), conn)

    if len(analysis_runs) == 0:
        return None

    # TODO better way than looping over each of these? move to sql query?
    analysis_start_times = analysis_runs.run_at.unique()
    seg_runs = []
    for run_at in analysis_start_times:
        seg_runs.append(pd.read_sql_query('SELECT * FROM segmentation_runs ' +
            "WHERE run_at = '{}'".format(pd.Timestamp(run_at)), conn))

        # TODO maybe merge w/ analysis_code (would have to yield multiple rows
        # per segmentation run when multiple code versions referenced)

    seg_runs = pd.concat(seg_runs, ignore_index=True)
    if len(seg_runs) == 0:
        return None

    seg_runs = seg_runs.merge(analysis_runs)
    seg_runs.sort_values('run_at', inplace=True)
    return seg_runs


def is_thorsync_dir(d, verbose=False):
    """True if dir has expected ThorSync outputs, False otherwise.
    """
    if not isdir(d):
        return False
    
    files = {f for f in listdir(d)}

    have_settings = False
    have_h5 = False
    for f in files:
        # checking for substring
        if 'ThorRealTimeDataSettings.xml' in f:
            have_settings = True
        if '.h5':
            have_h5 = True

    if verbose:
        print('have_settings:', have_settings)
        print('have_h5:', have_h5)

    return have_h5 and have_settings


def is_thorimage_dir(d, verbose=False):
    """True if dir has expected ThorImage outputs, False otherwise.

    Looks for .raw not any TIFFs now.
    """
    if not isdir(d):
        return False
    
    files = {f for f in listdir(d)}

    have_xml = False
    have_raw = False
    # TODO support tif output case(s) as well
    #have_processed_tiff = False
    for f in files:
        if f == 'Experiment.xml':
            have_xml = True
        elif f == 'Image_0001_0001.raw':
            have_raw = True
        #elif f == split(d)[-1] + '_ChanA.tif':
        #    have_processed_tiff = True

    if verbose:
        print('have_xml:', have_xml)
        print('have_raw:', have_raw)
        if have_xml and not have_raw:
            print('all dir contents:')
            pprint.pprint(files)

    if have_xml and have_raw:
        return True
    else:
        return False


def _filtered_subdirs(parent_dir, filter_funcs, exclusive=True, verbose=False):
    """Takes dir and indicator func(s) to subdirs satisfying them.

    Output is a flat list of directories if filter_funcs is a function.

    If it is a list of funcs, output has the same length, with each element
    a list of satisfying directories.
    """
    parent_dir = normpath(parent_dir)

    try:
        _ = iter(filter_funcs)
    except TypeError:
        filter_funcs = [filter_funcs]

    # [[]] * len(filter_funcs) was the inital way I tried this, but the inner
    # lists all end up referring to the same object.
    all_filtered_subdirs = []
    for _ in range(len(filter_funcs)):
        all_filtered_subdirs.append([])

    for d in glob.glob(f'{parent_dir}{sep}*{sep}'):
        if verbose:
            print(d)

        for fn, filtered_subdirs in zip(filter_funcs, all_filtered_subdirs):
            if verbose:
                print(fn.__name__)

            if verbose:
                try:
                    val = fn(d, verbose=True)
                except TypeError:
                    val = fn(d)
            else:
                val = fn(d)

            if verbose:
                print(val)

            if val:
                filtered_subdirs.append(d[:-1])
                if exclusive:
                    break

        if verbose:
            print('')

    if len(filter_funcs) == 1:
        all_filtered_subdirs = all_filtered_subdirs[0]

    return all_filtered_subdirs


def thorimage_subdirs(parent_dir):
    """
    Returns a list of any immediate child directories of `parent_dir` that have
    all expected ThorImage outputs.
    """
    return _filtered_subdirs(parent_dir, is_thorimage_dir)


def thorsync_subdirs(parent_dir):
    """Returns a list of any immediate child directories of `parent_dir`
    that have all expected ThorSync outputs.
    """
    return _filtered_subdirs(parent_dir, is_thorsync_dir)


def pair_thor_dirs(thorimage_dirs, thorsync_dirs, use_mtime=False,
    use_ranking=True, check_against_naming_conv=True, verbose=False):
    """
    Takes lists (not necessarily same len) of dirs, and returns a list of
    lits of matching (ThorImage, ThorSync) dirs (sorted by experiment time).

    Raises ValueError if two dirs of one type match to the same one of the
    other, but just returns shorter list of pairs if some matches can not be
    made.
    """
    if use_ranking:
        if len(thorimage_dirs) != len(thorsync_dirs):
            raise ValueError('can only pair with ranking when equal # dirs')

    thorimage_times = {d: get_thorimage_time(d, use_mtime=use_mtime)
        for d in thorimage_dirs}

    thorsync_times = {d: get_thorsync_time(d) for d in thorsync_dirs}

    thorimage_dirs = np.array(
        sorted(thorimage_dirs, key=lambda x: thorimage_times[x])
    )
    thorsync_dirs = np.array(
        sorted(thorsync_dirs, key=lambda x: thorsync_times[x])
    )

    if use_ranking:
        pairs = list(zip(thorimage_dirs, thorsync_dirs))
    else:
        from scipy.optimize import linear_sum_assignment

        # TODO maybe call scipy func on pandas obj w/ dirs as labels?
        costs = np.empty((len(thorimage_dirs), len(thorsync_dirs))) * np.nan
        for i, tid in enumerate(thorimage_dirs):
            ti_time = thorimage_times[tid]
            if verbose:
                print('tid:', tid)
                print('ti_time:', ti_time)

            for j, tsd in enumerate(thorsync_dirs):
                ts_time = thorsync_times[tsd]

                cost = (ts_time - ti_time).total_seconds()

                if verbose:
                    print(' tsd:', tsd)
                    print('  ts_time:', ts_time)
                    print('  cost (ts - ti):', cost)

                # Since ts time should be larger, but only if comparing XML TI
                # time w/ TS mtime (which gets changed as XML seems to be
                # written as experiment is finishing / in progress).
                if use_mtime:
                    cost = abs(cost)

                elif cost < 0:
                    # TODO will probably just need to make this a large const
                    # inf seems to make the scipy imp fail. some imp it works
                    # with?
                    #cost = np.inf
                    cost = 1e7

                costs[i,j] = cost

            if verbose:
                print('')

        ti_idx, ts_idx = linear_sum_assignment(costs)
        print(costs)
        print(ti_idx)
        print(ts_idx)
        pairs = list(zip(thorimage_dirs[ti_idx], thorsync_dirs[ts_idx]))

    # TODO TODO or just return these (flag to do so?)?
    if check_against_naming_conv:
        ti_last_parts = [split(tid)[-1] for tid, _ in pairs]

        thorimage_nums = []
        not_all_old_fmt = False
        for tp in ti_last_parts:
            num = old_fmt_thorimage_num(tp)
            if pd.isnull(num):
                not_all_old_fmt = True
                break
            thorimage_nums.append(num)

        if not_all_old_fmt:
            try:
                thorimage_nums = [new_fmt_thorimage_num(d)
                    for d in ti_last_parts]
            except ValueError as e:
                # (changing error type so it isn't caught, w/ other ValueErrors)
                raise AssertionError(str(e))

        if len(thorimage_nums) > len(set(thorimage_nums)):
            raise AssertionError('thorimage nums were not unique')

        thorsync_nums = [thorsync_num(split(tsd)[-1]) for _, tsd in pairs]

        # Ranking rather than straight comparison in case there is an offset.
        ti_rankings = np.argsort(thorimage_nums)
        ts_rankings = np.argsort(thorsync_nums)
        if not np.array_equal(ti_rankings, ts_rankings):
            raise AssertionError('time based rankings inconsistent w/ '
                'file name convention rankings')
        # TODO maybe also re-order pairs by these rankings? or by their own,
        # to also include case where not check_against... ?

        return pairs

    """
    thorimage_times = {d: get_thorimage_time(d) for d in thorimage_dirs}
    thorsync_times = {d: get_thorsync_time(d) for d in thorsync_dirs}

    image_and_sync_pairs = []
    matched_dirs = set()
    # TODO make sure this order is going the way i want
    for tid in sorted(thorimage_dirs, key=lambda x: thorimage_times[x]):
        ti_time = thorimage_times[tid]
        if verbose:
            print('tid:', tid)
            print('ti_time:', ti_time)

        # Seems ThorImage time (from TI XML) is always before ThorSync time
        # (from mtime of TS XML), so going to look for closest mtime.
        # TODO could also warn / fail if closest ti mtime to ts mtime
        # is inconsistent? or just use that?
        # TODO or just use numbers in names? or default to that / warn/fail if
        # not consistent?

        # TODO TODO would need to modify this alg to handle many cases
        # where there are mismatched #'s of recordings
        # (first tid will get the tsd, even if another tid is closer)
        # scipy.optimize.linear_sum_assignment looks interesting, but
        # not sure it can handle 

        min_positive_td = None
        closest_tsd = None
        for tsd in thorsync_dirs:
            ts_time = thorsync_times[tsd]
            td = (ts_time - ti_time).total_seconds()

            if verbose:
                print(' tsd:', tsd)
                print('  ts_time:', ts_time)
                print('  td (ts - ti):', td)

            # Since ts_time should be larger.
            if td < 0:
                continue

            if min_positive_td is None or td < min_positive_td:
                min_positive_td = td
                closest_tsd = tsd

            '''
            # didn't seem to work at all for newer output ~10/2019
            if abs(td) < time_mismatch_cutoff_s:
                if tid in matched_dirs or tsd in matched_dirs:
                    raise ValueError(f'either {tid} or {tsd} was already '
                        f'matched. existing pairs:\n{matched_dirs}')

                image_and_sync_pairs.append((tid, tsd))
                matched_dirs.add(tid)
                matched_dirs.add(tsd)
            '''

            matched_dirs.add(tid)
            matched_dirs.add(tsd)

        if verbose:
            print('')

    return image_and_sync_pairs
    """


def pair_thor_subdirs(parent_dir, verbose=False):
    """
    Raises ValueError when pair_thor_dirs does.
    """
    thorimage_dirs, thorsync_dirs = _filtered_subdirs(parent_dir,
        (is_thorimage_dir, is_thorsync_dir), verbose=False #verbose
    )
    if verbose:
        print('thorimage_dirs:')
        pprint.pprint(thorimage_dirs)
        print('thorsync_dirs:')
        pprint.pprint(thorsync_dirs)

    return pair_thor_dirs(thorimage_dirs, thorsync_dirs, verbose=True)


# TODO still work w/ parens added around initial .+ ? i want to match the parent
# id...
shared_subrecording_regex = '(.+)_\db\d_from_(nr|rig)'
def is_subrecording(thorimage_id):
    """
    Returns whether a recording id matches my GUIs naming convention for the
    "sub-recordings" it can create.
    """
    if re.search(shared_subrecording_regex + '$', thorimage_id):
        return True
    else:
        return False


def is_subrecording_tiff(tiff_filename):
    """
    Takes a TIFF filename to whether it matches the GUI's naming convention for
    the "sub-recordings" it can create.
    """
    # TODO technically, nr|rig should be same across two...
    if re.search(shared_subrecording_regex + '_(nr|rig).tif$', tiff_filename):
        return True
    else:
        return False


def subrecording_tiff_blocks(tiff_filename):
    """Returns tuple of int (start, stop) block numbers subrecording contains.

    Block numbers start at 0.

    Requires that is_subrecording_tiff(tiff_filename) would return True.
    """
    parts = tiff_filename.split('_')[-4].split('b')

    first_block = int(parts[0]) - 1
    last_block = int(parts[1]) - 1

    return first_block, last_block


def subrecording_tiff_blocks_df(series):
    """Takes a series w/ TIFF name in series.name to (start, stop) block nums.

    (series.name must be a TIFF path)

    Same behavior as `subrecording_tiff_blocks`.
    """
    # TODO maybe fail in this case?
    if not series.is_subrecording:
        return None, None

    tiff_filename = series.name
    first_block, last_block = subrecording_tiff_blocks(tiff_filename)
    return first_block, last_block
    '''
    return {
        'first_block': first_block,
        'last_block': last_block
    }
    '''


def parent_recording_id(tiffname_or_thorimage_id):
    """Returns recording id for recording subrecording was derived from.

    Input can be a TIFF filename or recording id.
    """
    last_part = split(tiffname_or_thorimage_id)[1]
    match = re.search(shared_subrecording_regex, last_part)
    if match is None:
        raise ValueError('not a subrecording')
    return match.group(1)
        

def accepted_blocks(analysis_run_at, verbose=False):
    """
    """
    global conn
    if conn is None:
        conn = get_db_conn()

    if verbose:
        print('entering accepted_blocks')

    analysis_run_at = pd.Timestamp(analysis_run_at)
    presentations = pd.read_sql_query('SELECT presentation_id, ' +
        'comparison, presentation_accepted FROM presentations WHERE ' +
        "analysis = '{}'".format(analysis_run_at), conn,
        index_col='comparison')
    # TODO any of stuff below behave differently if index is comparison
    # (vs. default range index)? groupby('comparison')?

    analysis_run = pd.read_sql_query('SELECT accepted, input_filename, ' +
        "recording_from FROM analysis_runs WHERE run_at = '{}'".format(
        analysis_run_at), conn)
    assert len(analysis_run) == 1
    analysis_run = analysis_run.iloc[0]
    recording_from = analysis_run.recording_from
    input_filename = analysis_run.input_filename
    all_blocks_accepted = analysis_run.accepted

    # TODO TODO make sure block bounds are loaded into db from gui first, if
    # they changed in the gsheet. otherwise, will be stuck using old values, and
    # this function will not behave correctly
    # TODO TODO this has exactly the same problem canonical_segmentation
    # currently has: only one of each *_block per recording start time =>
    # sub-recordings will clobber each other. fix!
    # (currently just working around w/ subrecording tif filename hack)
    recording = pd.read_sql_query('SELECT thorimage_path, first_block, ' +
        "last_block FROM recordings WHERE started_at = '{}'".format(
        recording_from), conn)

    assert len(recording) == 1
    recording = recording.iloc[0]

    # TODO delete this if not going to use it to calculate uploaded_block_info
    if len(presentations) > 0:
        presentations_with_responses = pd.read_sql_query('SELECT ' +
            'presentation_id FROM responses WHERE segmentation_run = ' +
            "'{}'".format(analysis_run_at), conn)
        # TODO faster to check isin if this is a set?
    #

    # TODO TODO implement some kind of handling of sub-recordings in db
    # and get rid of this hack
    #print(input_filename)
    if is_subrecording_tiff(input_filename):
        first_block, last_block = subrecording_tiff_blocks(input_filename)

        if verbose:
            print(input_filename, 'belonged to a sub-recording')

    else:
        if recording.last_block is None or recording.first_block is None:
            # TODO maybe generate it in this case?
            raise ValueError(('no block info in db for recording_from = {} ({})'
                ).format(recording_from, recording.thorimage_path))

        first_block = recording.first_block
        last_block = recording.last_block

    n_blocks = last_block - first_block + 1
    expected_comparisons = list(range(n_blocks))

    # TODO delete these prints. for debugging.
    if verbose:
        print('presentations:', presentations)
        print('expected_comparisons:', expected_comparisons)
        print('all_blocks_accepted:', all_blocks_accepted)
    #
    # TODO TODO TODO check that upload will keep comparison numbered as blocks
    # are, so that missing comparisons numbers can be imputed with False here
    # (well, comparison numbering should probably start w/ 0 at first_block)

    # TODO TODO test cases where not all blocks were uploaded at all, where some
    # where not uploaded and some are explicitly marked not accepted, and where
    # all blocks rejected are explicitly so

    if pd.notnull(all_blocks_accepted):
        fill_value = all_blocks_accepted
    else:
        fill_value = False

    def block_accepted(presentation_df):
        null = pd.isnull(presentation_df.presentation_accepted)
        if null.any():
            assert null.all()
            return fill_value

        accepted = presentation_df.presentation_accepted
        if accepted.any():
            assert accepted.all()
            return True
        else:
            return False

    '''
    null_presentation_accepted = \
        pd.isnull(presentations.presentation_accepted)
    if null_presentation_accepted.any():
        if verbose:
            print('at least one presentation was null')
        # TODO fix db w/ a script or just always check for old way of doing it?
        # fixing db would mean translating all analysis_runs.accepted into
        # presentations.presentation_accepted and then deleting
        # analysis_runs.accepted column

        assert null_presentation_accepted.all(), 'not all null'
        assert not pd.isnull(all_blocks_accepted),'all_blocks_accepted null'

        if all_blocks_accepted:
            accepted = [True] * n_blocks
        else:
            accepted = [False] * n_blocks
    else:
        if verbose:
            print('no presentations were null')
    '''
    # TODO make sure sorted by comparison #. groupby ensure that?
    accepted = presentations.groupby('comparison'
        ).agg(block_accepted).presentation_accepted
    accepted.name = 'comparison_accepted'
    assert len(accepted.shape) == 1, 'accepted was not a Series'

    if verbose:
        print('accepted before filling missing values:', accepted)

    if (((accepted == True).any() and all_blocks_accepted == False) or
        ((accepted == False).any() and all_blocks_accepted)):
        # TODO maybe just correct db in this case?
        # (set analysis_run.accepted to null and keep presentation_accepted
        # if inconsistent / fill them from analysis_run.accepted if missing)
        #raise ValueError('inconsistent accept labels')
        warnings.warn('inconsistent accept labels. ' +
            'nulling analysis_runs.accepted in corresponding row.')

        # TODO TODO test this!
        sql = ('UPDATE presentations SET presentation_accepted = {} WHERE ' +
            "analysis = '{}' AND presentation_accepted IS NULL").format(
            fill_value, analysis_run_at)
        ret = conn.execute(sql)
        # TODO if i'm gonna call this multiple times, maybe just factor it into
        # a fn
        presentations_after_update = pd.read_sql_query(
            'SELECT presentation_id, ' +
            'comparison, presentation_accepted FROM presentations WHERE ' +
            "analysis = '{}'".format(analysis_run_at), conn,
            index_col='comparison')
        if verbose:
            print('Presentations after filling w/ all_blocks_accepted:')
            print(presentations_after_update)

        sql = ('UPDATE analysis_runs SET accepted = NULL WHERE run_at = ' +
            "'{}'").format(analysis_run_at)
        ret = conn.execute(sql)

    # TODO TODO TODO are this case + all_blocks_accepted=False case in if
    # above the only two instances where the block info is not uploaded (or
    # should be, assuming no accept of non-uploaded experiment)
    for c in expected_comparisons:
        if c not in accepted.index:
            accepted.loc[c] = fill_value

    accepted = accepted.to_list()

    # TODO TODO TODO TODO also calculate and return uploaded_block_info
    # based on whether a given block has (all) of it's presentations and
    # responses entries (whether accepted or not)
    if verbose:
        print('leaving accepted_blocks\n')
    return accepted


def print_all_accepted_blocks():
    """Just for testing behavior of accepted_blocks fn.
    """
    global conn
    if conn is None:
        conn = get_db_conn()

    analysis_runs = pd.read_sql_query('SELECT run_at FROM segmentation_runs',
        conn).run_at

    for r in analysis_runs:
        try:
            print('{}: {}'.format(r, accepted_blocks(r)))
        except ValueError as e:
            print(e)
            continue
        #import ipdb; ipdb.set_trace()

    import ipdb; ipdb.set_trace()


def _xmlroot(xml_path):
    return etree.parse(xml_path).getroot()


# TODO maybe rename to exclude get_ prefix, to be consistent w/
# thorimage_dir(...) and others above?
def get_thorimage_xml_path(thorimage_dir):
    """Takes ThorImage output dir to path to its XML output.
    """
    return join(thorimage_dir, 'Experiment.xml')


def get_thorimage_xmlroot(thorimage_dir):
    """Takes ThorImage output dir to object w/ XML data.
    """
    xml_path = get_thorimage_xml_path(thorimage_dir)
    return _xmlroot(xml_path)


def get_thorimage_time_xml(xml):
    """Takes etree XML root object to recording start time.

    XML object should be as returned by `get_thorimage_xmlroot`.
    """
    date_ele = xml.find('Date')
    from_date = datetime.strptime(date_ele.attrib['date'], '%m/%d/%Y %H:%M:%S')
    from_utime = datetime.fromtimestamp(float(date_ele.attrib['uTime']))
    assert (from_date - from_utime).total_seconds() < 1
    return from_utime


def get_thorimage_time(thorimage_dir, use_mtime=False):
    """Takes ThorImage directory to recording start time (from XML).
    """
    xml_path = get_thorimage_xml_path(thorimage_dir)

    # TODO delete. for debugging matching.
    '''
    xml = _xmlroot(xml_path)
    print(thorimage_dir)
    print(get_thorimage_time_xml(xml))
    print(datetime.fromtimestamp(getmtime(xml_path)))
    print('')
    '''
    #
    if not use_mtime:
        xml = _xmlroot(xml_path)
        return get_thorimage_time_xml(xml)
    else:
        return datetime.fromtimestamp(getmtime(xml_path))


def get_thorsync_time(thorsync_dir):
    """Returns modification time of ThorSync XML.

    Not perfect, but it doesn't seem any ThorSync outputs have timestamps.
    """
    syncxml = join(thorsync_dir, 'ThorRealTimeDataSettings.xml')
    return datetime.fromtimestamp(getmtime(syncxml))


def get_thorimage_dims_xml(xml):
    """Takes etree XML root object to (xy, z, c) dimensions of movie.

    XML object should be as returned by `get_thorimage_xmlroot`.
    """
    lsm_attribs = xml.find('LSM').attrib
    x = int(lsm_attribs['pixelX'])
    y = int(lsm_attribs['pixelY'])
    xy = (x,y)

    # TODO make this None unless z-stepping seems to be enabled
    # + check this variable actually indicates output steps
    #int(xml.find('ZStage').attrib['steps'])
    z = None
    c = None

    return xy, z, c


def get_thorimage_pixelsize_xml(xml):
    """Takes etree XML root object to XY pixel size in um.

    Pixel size in X is the same as pixel size in Y.

    XML object should be as returned by `get_thorimage_xmlroot`.
    """
    # TODO does thorimage (and their xml) even support unequal x and y?
    # TODO support z here?
    return float(xml.find('LSM').attrib['pixelSizeUM'])


def get_thorimage_fps_xml(xml):
    """Takes etree XML root object to (after-any-averaging) fps of recording.

    XML object should be as returned by `get_thorimage_xmlroot`.
    """
    lsm_attribs = xml.find('LSM').attrib
    raw_fps = float(lsm_attribs['frameRate'])
    # TODO is this correct handling of averageMode?
    average_mode = int(lsm_attribs['averageMode'])
    if average_mode == 0:
        n_averaged_frames = 1
    else:
        n_averaged_frames = int(lsm_attribs['averageNum'])
    saved_fps = raw_fps / n_averaged_frames
    return saved_fps


def get_thorimage_fps(thorimage_directory):
    """Takes ThorImage dir to (after-any-averaging) fps of recording.
    """
    xml = get_thorimage_xmlroot(thorimage_directory)
    return get_thorimage_fps_xml(xml)


# TODO maybe delete / refactor to use fns above
def tif2xml_root(filename):
    """Returns etree root of ThorImage XML settings from TIFF filename.

    Path can be to analysis output directory, as long as raw data directory
    exists.
    """
    if filename.startswith(analysis_output_root()):
        filename = filename.replace(analysis_output_root(), raw_data_root())

    parts = filename.split(sep)
    thorimage_id = '_'.join(parts[-1].split('_')[:-1])

    xml_fname = sep.join(parts[:-2] + [thorimage_id, 'Experiment.xml'])
    return _xmlroot(xml_fname)


# TODO TODO rename this one to make it clear why it's diff from above
# + how to use it (or just delete one...)
def fps_from_thor(df):
    """Takes a DataFrame and returns fps from ThorImage XML.
    
    df must have a thorimage_dir column (that can be either a relative or
    absolute path, as long as it's under raw_data_root)

    Only the path in the first row is used.
    """
    # TODO assert unique first?
    thorimage_dir = df['thorimage_path'].iat[0]
    # TODO maybe factor into something that ensures path has a certain prefix
    # that maybe also validates right # parts?
    thorimage_dir = join(raw_data_root(), *thorimage_dir.split('/')[-3:])
    fps = get_thorimage_fps(thorimage_dir)
    return fps


def cnmf_metadata_from_thor(filename):
    """Takes TIF filename to key settings from XML needed for CNMF.
    """
    xml_root = tif2xml_root(filename)
    fps = get_thorimage_fps_xml(xml_root)
    # "spatial resolution of FOV in pixels per um" "(float, float)"
    # TODO do they really mean pixel/um, not um/pixel?
    pixels_per_um = 1 / get_thorimage_pixelsize_xml(xml_root)
    dxy = (pixels_per_um, pixels_per_um)
    # TODO maybe load dims anyway?
    return {'fr': fps, 'dxy': dxy}


def load_thorimage_metadata(thorimage_directory):
    """Returns (fps, xy, z, c, raw_output_path) for ThorImage dir.
    """
    xml = get_thorimage_xmlroot(thorimage_directory)

    fps = get_thorimage_fps_xml(xml)
    xy, z, c = get_thorimage_dims_xml(xml)
    imaging_file = join(thorimage_directory, 'Image_0001_0001.raw')

    return fps, xy, z, c, imaging_file


def read_movie(thorimage_dir):
    """Returns (t,x,y) indexed timeseries.
    """
    fps, xy, z, c, imaging_file = load_thorimage_metadata(thorimage_dir)
    x, y = xy

    # From ThorImage manual: "unsigned, 16-bit, with little-endian byte-order"
    dtype = np.dtype('<u2')

    with open(imaging_file, 'rb') as f:
        data = np.fromfile(f, dtype=dtype)

    n_frame_pixels = x * y
    n_frames = len(data) // n_frame_pixels
    assert len(data) % n_frame_pixels == 0, 'apparent incomplete frames'

    data = np.reshape(data, (n_frames, x, y))
    return data


def write_tiff(tiff_filename, movie):
    """Write a TIFF loading the same as the TIFFs we create with ImageJ.

    TIFFs are written in big-endian byte order to be readable by `imread_big`
    from MATLAB file exchange.

    Metadata may not be correct.
    """
    import tifffile

    dtype = movie.dtype
    if not (dtype.itemsize == 2 and
        np.issubdtype(dtype, np.unsignedinteger)):

        raise ValueError('movie must have uint16 dtype')

    if dtype.byteorder == '|':
        raise ValueError('movie must have explicit endianness')

    # If little-endian, convert to big-endian before saving TIFF, almost
    # exclusively for the benefit of MATLAB imread_big, which doesn't seem
    # able to discern the byteorder.
    if (dtype.byteorder == '<' or
        (dtype.byteorder == '=' and sys.byteorder == 'little')):
        movie = movie.byteswap().newbyteorder()
    else:
        assert dtype.byteorder == '>'
    
    # TODO actually make sure any metadata we use is the same
    # TODO maybe just always do test from test_readraw here?
    # (or w/ flag to disable the check)
    tifffile.imsave(tiff_filename, movie, imagej=True)


def full_frame_avg_trace(movie):
    """Takes a (t,x,y[,z]) movie to t-length vector of frame averages.
    """
    # Averages all dims but first, which is assumed to be time.
    return np.mean(movie, axis=tuple(range(1, movie.ndim)))


def crop_to_coord_bbox(matrix, coords, margin=0):
    """Returns matrix cropped to bbox of coords and bounds.
    """
    x_min, y_min = coords.min(axis=0)
    x_max, y_max = coords.max(axis=0)

    assert x_min >= 0 and y_min >= 0, \
        f'mins must be >= 0 (x_min={x_min}, y_min={y_min})'

    # TODO might need to fix this / fns that use this such a that 
    # coord limits are actually < matrix dims, rather than <=
    '''
    assert x_max < matrix.shape[0] and y_max < matrix.shape[1], \
        (f'maxes must be < matrix shape = {matrix.shape} (x_max={x_max}' +
        f', y_max={y_max}')
    '''
    assert x_max <= matrix.shape[0] and y_max <= matrix.shape[1], \
        (f'maxes must be <= matrix shape = {matrix.shape} (x_max={x_max}' +
        f', y_max={y_max}')

    # Keeping min at 0 to prevent slicing error in that case
    # (I think it will be empty, w/ -1:2, for instance)
    # Capping max not necessary to prevent err, but to make behavior of bounds
    # consistent on both edges.
    x_min = max(0, x_min - margin)
    x_max = min(x_max + margin, matrix.shape[0] - 1)
    y_min = max(0, y_min - margin)
    y_max = min(y_max + margin, matrix.shape[1] - 1)

    cropped = matrix[x_min:x_max+1, y_min:y_max+1]
    return cropped, ((x_min, x_max), (y_min, y_max))


def crop_to_nonzero(matrix, margin=0):
    """
    Returns a matrix just large enough to contain the non-zero elements of the
    input, and the bounding box coordinates to embed this matrix in a matrix
    with indices from (0,0) to the max coordinates in the input matrix.
    """
    coords = np.argwhere(matrix > 0)
    return crop_to_coord_bbox(matrix, coords, margin=margin)


# TODO better name?
def db_row2footprint(db_row, shape=None):
    """Returns dense array w/ footprint from row in cells table.
    """
    from scipy.sparse import coo_matrix
    weights, x_coords, y_coords = db_row[['weights','x_coords','y_coords']]
    # TODO maybe read shape from db / metadata on disk? / merging w/ other
    # tables (possible?)?
    footprint = np.array(coo_matrix((weights, (x_coords, y_coords)),
        shape=shape).todense()).T
    return footprint


def db_footprints2array(df, shape):
    """Returns footprints in an array of dims (shape + (n_footprints,)).
    """
    return np.stack([db_row2footprint(r, shape) for _, r in df.iterrows()],
        axis=-1)


# TODO test w/ mpl / cv2 contours that never see ij to see if transpose is
# necessary!
def contour2mask(contour, shape):
    """Returns a boolean mask True inside contour and False outside.
    """
    import cv2
    # TODO any checking of contour necessary for it to be well behaved in
    # opencv?
    mask = np.zeros(shape, np.uint8)
    # TODO draw into a sparse array maybe? or convert after?
    cv2.drawContours(mask, [contour.astype(np.int32)], 0, 1, -1)
    # TODO TODO TODO investigate need for this transpose
    # (imagej contour repr specific? maybe load to contours w/ dims swapped them
    # call this fn w/o transpose?)
    # (was it somehow still a product of x_coords / y_coords being swapped in
    # db?)
    # not just b/c reshaping to something expecting F order CNMF stuff?
    # didn't correct time averaging w/in roi also require this?
    return mask.astype('bool')


def ijrois2masks(ijrois, shape, dims_as_cnmf=False):
    """
    Transforms ROIs loaded from my ijroi fork to an array full of boolean masks,
    of dimensions (shape + (n_rois,)).
    """
    # TODO maybe index final pandas thing by ijroi name (before .roi prefix)
    # (or just return np array indexed as CNMF "A" is)

    # TODO test + fix. if this is duplicating logic of imagej2py_coords, try to
    # move into there or somehow else only encode that imagej is transposed 
    # wrt other things in ONE place (that was the point of those x2y_coords
    # fns...)
    assert len(shape) == 2 and shape[0] == shape[1], \
        'not sure shape dims should be reversed, so must be symmetric'

    masks = [imagej2py_coords(contour2mask(c, shape[::-1])) for _, c in ijrois]
    masks = np.stack(masks, axis=-1)
    # (actually, putting off the below for now. just gonna not also reshape this
    # output as we currently reshape CNMF A before using it for other stuff)
    if dims_as_cnmf:
        # TODO check that reshaping is not breaking association to components
        # (that it is equivalent to repeating reshape w/in each component and
        # then stacking)
        # TODO TODO conform shape to cnmf output shape (what's that dim order?)
        # n_pixels x n_components, w/ n_pixels reshaped from ixj image "in F
        # order"
        #import ipdb; ipdb.set_trace()
        raise NotImplementedError
    # TODO maybe normalize here?
    # (and if normalizing, might as well change dtype to match cnmf output?)
    # and worth casting type to bool, rather than keeping 0/1 uint8 array?
    return masks


def imagej2py_coords(array):
    """
    Since ijroi source seems to have Y as first coord and X as second.
    """
    return array.T


def py2imagej_coords(array):
    """
    Since ijroi source seems to have Y as first coord and X as second.
    """
    return array.T


# TODO TODO probably make a corresponding fn to do the inverse
# (or is one of these not necessary? in one dir, is order='C' and order
def footprints_to_flat_cnmf_dims(footprints):
    """Takes array of (x, y[, z], n_footprints) to (n_pixels, n_footprints).

    There is more than one way this reshaping can be done, and this produces
    output as CNMF expects it.
    """
    frame_pixels = np.prod(footprints.shape[:-1])
    n_footprints = footprints.shape[-1]
    # TODO TODO is this supposed to be order='F' or order='C' matter?
    # wrong setting equivalent to transpose?
    # what's the appropriate test (make unit?)?
    return np.reshape(footprints, (frame_pixels, n_footprints), order='F')


def extract_traces_boolean_footprints(movie, footprints):
    """
    Averages the movie within each boolean mask in footprints
    to make a matrix of traces (n_frames x n_footprints).
    """
    assert footprints.dtype.kind != 'f', 'float footprints are not boolean'
    assert footprints.max() == 1, 'footprints not boolean'
    assert footprints.min() == 0, 'footprints not boolean'
    n_spatial_dims = len(footprints.shape) - 1
    spatial_dims = tuple(range(n_spatial_dims))
    assert np.any(footprints, axis=spatial_dims).all(), 'some zero footprints'
    slices = (slice(None),) * n_spatial_dims
    n_frames = movie.shape[0]
    # TODO vectorized way to do this?
    n_footprints = footprints.shape[-1]
    traces = np.empty((n_frames, n_footprints)) * np.nan
    print('extracting traces from boolean masks...', end='', flush=True)
    for i in range(n_footprints):
        mask = footprints[slices + (i,)]
        # TODO compare time of this to sparse matrix dot product?
        # + time of MaskedArray->mean w/ mask expanded by n_frames?

        # TODO TODO is this correct? check
        # axis=1 because movie[:, mask] only has two dims (frames x pixels)
        trace = np.mean(movie[:, mask], axis=1)
        assert len(trace.shape) == 1 and len(trace) == n_frames
        traces[:, i] = trace
    print(' done')
    return traces


def exp_decay(t, scale, tau, offset):
    # TODO is this the usual definition of tau (as in RC time constant?)
    return scale * np.exp(-t / tau) + offset


# TODO call for each odor onset (after fixed onset period?)
# est onset period? est rise kinetics jointly? how does cnmf do it?
def fit_exp_decay(signal, sampling_rate=None, times=None, numerical_scale=1.0):
    """Returns fit parameters for an exponential decay in the input signal.

    Args:
        signal (1 dimensional np.ndarray): time series, beginning at decay onset
        sampling_rate (float): sampling rate in Hz
    """
    from scipy.optimize import curve_fit

    if sampling_rate is None and times is None:
        raise ValueError('pass either sampling_rate or times as keyword arg')

    if sampling_rate is not None:
        sampling_interval = 1 / sampling_rate
        n_samples = len(signal)
        end_time = n_samples * sampling_interval
        times = np.linspace(0, end_time, num=n_samples, endpoint=True)

    # TODO make sure input is not modified here. copy?
    signal = signal * numerical_scale

    # TODO constrain params somehow? for example, so scale stays positive
    popt, pcov = curve_fit(exp_decay, times, signal,
        p0=(1.8 * numerical_scale, 5.0, 0.0 * numerical_scale))

    # TODO is this correct to scale after converting variance to stddev?
    sigmas = np.sqrt(np.diag(pcov))
    sigmas[0] = sigmas[0] / numerical_scale
    # skipping tau, which shouldn't need to change (?)
    sigmas[2] = sigmas[2] / numerical_scale

    # TODO only keep this if signal is modified s.t. it affects calling fn.
    # in this case, maybe still just copy above?
    signal = signal / numerical_scale

    scale, tau, offset = popt
    return (scale / numerical_scale, tau, offset / numerical_scale), sigmas


def latest_analysis(verbose=False):
    global conn
    if conn is None:
        conn = get_db_conn()

    # TODO sql based command to get analysis info for stuff that has its
    # timestamp in segmentation_runs, to condense these calls to one?
    seg_runs = pd.read_sql_query('SELECT run_at FROM segmentation_runs',
        conn)
    analysis_runs = pd.read_sql('analysis_runs', conn)
    seg_runs = seg_runs.merge(analysis_runs)

    seg_runs.input_filename = seg_runs.input_filename.apply(lambda t:
        t.split('mb_team/analysis_output/')[1])

    # TODO TODO change all path handling to be relative to NAS_PREFIX.
    # don't store absolute paths (or if i must, also store what prefix is?)
    input_tifs = seg_runs.input_filename.unique()
    has_subrecordings = set()
    key2tiff = dict()
    # TODO decide whether i want this to be parent_id or thorimage_id
    # maybe make a kwarg flag to this fn to switch between them
    tiff2parent_id = dict()
    tiff_is_subrecording = dict()
    for tif in input_tifs:
        if verbose:
            print(tif, end='')

        date_fly_keypart = '/'.join(tif.split('/')[:2])
        thorimage_id = tiff_thorimage_id(tif)
        key = '{}/{}'.format(date_fly_keypart, thorimage_id)
        # Assuming this is 1:1 for now.
        key2tiff[key] = tif
        try:
            parent_id = parent_recording_id(tif)
            tiff_is_subrecording[tif] = True
            parent_key = '{}/{}'.format(date_fly_keypart, parent_id)
            has_subrecordings.add(parent_key)
            tiff2parent_id[tif] = parent_id

            if verbose:
                print(' (sub-recording of {})'.format(parent_key))

        # This is triggered if tif is not a sub-recording.
        except ValueError:
            tiff_is_subrecording[tif] = False
            tiff2parent_id[tif] = thorimage_id
            if verbose:
                print('')

    nonoverlapping_input_tifs = set(t for k, t in key2tiff.items()
                                 if k not in has_subrecordings)
    # set(input_tifs) - nonoverlapping_input_tifs

    # TODO if verbose, maybe also print stuff tossed for having subrecordings
    # as well as # rows tossed for not being accepted / stuff w/o analysis /
    # stuff w/o anything accepted

    # TODO between this and the above, make sure to handle (ignore) stuff that
    # doesn't have any analysis done.
    seg_runs = seg_runs[seg_runs.input_filename.isin(nonoverlapping_input_tifs)]

    # TODO TODO and if there are disjoint sets of accepted blocks, would ideally
    # return something indicating which analysis to get which block from?  would
    # effectively have to search per block/comparison, right?
    # TODO would ideally find the latest analysis that has the maximal
    # number of blocks accepted (per input file) (assuming just going to return
    # one analysis version per tif, rather than potentially a different one for
    # each block)
    seg_runs['n_accepted_blocks'] = seg_runs.run_at.apply(lambda r:
        sum(accepted_blocks(r)))
    accepted_runs = seg_runs[seg_runs.n_accepted_blocks > 0]

    latest_tif_analyses = accepted_runs.groupby('input_filename'
        ).run_at.max().to_frame()
    latest_tif_analyses['is_subrecording'] = \
        latest_tif_analyses.index.map(tiff_is_subrecording)

    subrec_blocks = latest_tif_analyses.apply(subrecording_tiff_blocks_df,
        axis=1, result_type='expand')
    latest_tif_analyses[['first_block','last_block']] = subrec_blocks

    latest_tif_analyses['thorimage_id'] = \
        latest_tif_analyses.index.map(tiff2parent_id)

    # TODO what format would make the most sense for the output?
    # which index? just trimmed input_filename? recording_from (+ block /
    # comparison)? (fly, date, [thorimage_id? recording_from?] (+ block...)
    # ?
    return latest_tif_analyses


def sql_timestamp_list(df):
    """
    df must have a column run_at, that is a pandas Timestamp type
    """
    timestamp_list = '({})'.format(', '.join(
        ["'{}'".format(x) for x in df.run_at]
    ))
    return timestamp_list


# TODO w/ this or a separate fn using this, print what we have formatted
# roughly like in data_tree in gui, so that i can check it against the gui
def latest_analysis_presentations(analysis_run_df):
    global conn
    if conn is None:
        conn = get_db_conn()

    # TODO maybe compare time of this to getting all and filtering locally
    # TODO at least once, compare the results of this to filtering locally
    # IS NOT DISTINCT FROM should also 
    presentations = pd.read_sql_query('SELECT * FROM presentations WHERE ' +
        '(presentation_accepted = TRUE OR presentation_accepted IS NULL) ' +
        'AND analysis IN ' + sql_timestamp_list(analysis_run_df), conn)

    # TODO TODO maybe just do a migration on the db to fix all comparisons
    # to not have to be renumbered, and fix gui(+populate_db?) so they don't
    # restart numbering across sub-recordings that come from same recording?

    # TODO TODO TODO test that this is behaving as expected
    # - is there only one place where presentatinos.analysis == row.run_at?
    #   assert that?
    # - might the sample things ever get incremented twice?
    for row in analysis_run_df[analysis_run_df.is_subrecording].itertuples():
        run_presentations = (presentations.analysis == row.run_at)
        presentations.loc[run_presentations, 'comparison'] = \
            presentations[run_presentations].comparison + int(row.first_block)

        # TODO check that these rows are also complete / valid

    # TODO use those check fns on these presentations, to make sure they are
    # full blocks and stuff

    # TODO ultimately, i want all of these latest_* functions to return a
    # dataframe without an analysis column (still return it, just in case it
    # becomes necessary later?)
    # (or at least i want to make sure that the other index columns can uniquely
    # refer to something, s.t. adding analysis to a drop_duplicates call does
    # not change the total # of returned de-duplicated rows)
    # TODO which index cols again?

    return presentations


def latest_analysis_footprints(analysis_run_df):
    global conn
    if conn is None:
        conn = get_db_conn()

    footprints = pd.read_sql_query(
        'SELECT * FROM cells WHERE segmentation_run IN ' +
        sql_timestamp_list(analysis_run_df), conn)
    return footprints


def latest_analysis_traces(df):
    """
    Input DataFrame must have a presentation_id column matching that in the db.
    This way, presentations already filtered to be the latest just get their
    responses assigned too them.
    """
    global conn
    if conn is None:
        conn = get_db_conn()

    responses = pd.read_sql_query(
        'SELECT * FROM responses WHERE presentation_id IN ' +
        '({})'.format(','.join([str(x) for x in df.presentation_id])), conn)
    # responses should by larger by a factor of # cells within each analysis run
    assert len(df) == len(responses.presentation_id.unique())
    return responses
    

response_stat_cols = [
    'exp_scale',
    'exp_tau',
    'exp_offset',
    'exp_scale_sigma',
    'exp_tau_sigma',
    'exp_offset_sigma',
    'avg_dff_5s',
    'avg_zchange_5s'
]
def latest_response_stats(*args):
    """
    """
    global conn
    if conn is None:
        conn = get_db_conn()

    index_cols = [
        'prep_date',
        'fly_num',
        'recording_from',
        'analysis',
        'comparison',
        'odor1',
        'odor2',
        'repeat_num'
    ]
    # TODO maybe just get cols db has and exclude from_onset or something?
    # just get all?
    presentation_cols_to_get = index_cols + response_stat_cols
    if len(args) == 0:
        db_presentations = pd.read_sql('presentations', conn,
            columns=(presentation_cols_to_get + ['presentation_id']))

    elif len(args) == 1:
        db_presentations = args[0]

    else:
        raise ValueError('too many arguments. expected 0 or 1')

    referenced_recordings = set(db_presentations['recording_from'].unique())

    if len(referenced_recordings) == 0:
        return

    db_analysis_runs = pd.read_sql('analysis_runs', conn,
        columns=['run_at', 'recording_from', 'accepted'])
    db_analysis_runs.set_index(['recording_from', 'run_at'],
        inplace=True)

    # Making sure not to get multiple presentation entries referencing the same
    # real presentation in any single recording.
    presentation_stats = []
    for r in referenced_recordings:
        # TODO are presentation->recording and presentation->
        # analysis_runs->recording inconsistent somehow?
        # TODO or is this an insertion order thing? rounding err?
        # maybe set_index is squashing stuff?
        # TODO maybe just stuff i'm skipping now somehow?

        # TODO TODO merge db_analysis_runs w/ recordings to get
        # thorimage_dir / id for troubleshooting?
        # TODO fix and delete try / except
        try:
            rec_analysis_runs = db_analysis_runs.loc[(r,)]
        except KeyError:
            # TODO should this maybe be an error?
            '''
            print(db_analysis_runs)
            print(referenced_recordings)
            print(r)
            import ipdb; ipdb.set_trace()
            '''
            warnings.warn('referenced recording not in analysis_runs!')
            continue

        # TODO TODO TODO switch to using presentations.presentation_accepted
        raise NotImplementedError
        rec_usable = rec_analysis_runs.accepted.any()

        rec_presentations = db_presentations[
            db_presentations.recording_from == r]

        # TODO maybe use other fns here to check it has all repeats / full
        # comparisons?

        for g, gdf in rec_presentations.groupby(
            ['comparison', 'odor1', 'odor2', 'repeat_num']):

            # TODO rename (maybe just check all response stats at this point...)
            # maybe just get most recent row that has *any* of them?
            # (otherwise would have to combine across rows...)
            has_exp_fit = gdf[gdf.exp_scale.notnull()]

            # TODO compute if no response stats?
            if len(has_exp_fit) == 0:
                continue

            most_recent_fit_idx = has_exp_fit.analysis.idxmax()
            most_recent_fit = has_exp_fit.loc[most_recent_fit_idx].copy()

            assert len(most_recent_fit.shape) == 1

            # TODO TODO TODO switch to using presentations.presentation_accepted
            raise NotImplementedError
            most_recent_fit['accepted'] = rec_usable

            # TODO TODO TODO clean up older fits on same data?
            # (delete from database)
            # (if no dependent objects...)
            # probably somewhere else...?

            presentation_stats.append(most_recent_fit.to_frame().T)

    if len(presentation_stats) == 0:
        return

    presentation_stats_df = pd.concat(presentation_stats, ignore_index=True)

    # TODO just convert all things that look like floats?

    for c in response_stat_cols:
        presentation_stats_df[c] = presentation_stats_df[c].astype('float64')

    for date_col in ('prep_date', 'recording_from'):
        presentation_stats_df[date_col] = \
            pd.to_datetime(presentation_stats_df[date_col])

    return presentation_stats_df


def n_expected_repeats(df):
    """Returns expected # repeats given DataFrame w/ repeat_num col.
    """
    max_repeat = df.repeat_num.max()
    return max_repeat + 1


# TODO TODO could now probably switch to using block metadata in recording table
# (n_repeats should be in there)
def missing_repeats(df, n_repeats=None):
    """
    Requires at least recording_from, comparison, name1, name2, and repeat_num
    columns. Can also take prep_date, fly_num, thorimage_id.
    """
    # TODO n_repeats default to 3 or None?
    if n_repeats is None:
        # TODO or should i require input is merged w/ recordings for stimuli
        # data file paths and then just load for n_repeats and stuff?
        n_repeats = n_expected_repeats(df)

    # Expect repeats to include {0,1,2} for 3 repeat experiments.
    expected_repeats = set(range(n_repeats))

    repeat_cols = []
    opt_repeat_cols = [
        'prep_date',
        'fly_num',
        'thorimage_id'
    ]
    for oc in opt_repeat_cols:
        if oc in df.columns:
            repeat_cols.append(oc)

    repeat_cols += [
        'recording_from',
        'comparison',
        'name1',
        'name2'#,
        #'log10_conc_vv1',
        #'log10_conc_vv2'
    ]
    # TODO some issue created by using float concs as a key?
    # TODO use odor ids instead?
    missing_repeat_dfs = []
    for g, gdf in df.groupby(repeat_cols):
        comparison_n_repeats = gdf.repeat_num.unique()

        no_extra_repeats = (gdf.repeat_num.value_counts() == 1).all()
        assert no_extra_repeats

        missing_repeats = [r for r in expected_repeats
            if r not in comparison_n_repeats]

        if len(missing_repeats) > 0:
            gmeta = gdf[repeat_cols].drop_duplicates().reset_index(drop=True)

        for r in missing_repeats:
            new_row = gmeta.copy()
            new_row['repeat_num'] = r
            missing_repeat_dfs.append(new_row)

    if len(missing_repeat_dfs) == 0:
        missing_repeats_df = \
            pd.DataFrame({r: [] for r in repeat_cols + ['repeat_num']})
    else:
        # TODO maybe merge w/ odor info so caller doesn't have to, if thats the
        # most useful for troubleshooting?
        missing_repeats_df = pd.concat(missing_repeat_dfs, ignore_index=True)

    missing_repeats_df.recording_from = \
        pd.to_datetime(missing_repeats_df.recording_from)

    # TODO should expected # blocks be passed in?

    return missing_repeats_df


def have_all_repeats(df, n_repeats=None):
    """
    Returns True if a recording has all blocks gsheet says it has, w/ full
    number of repeats for each. False otherwise.

    Requires at least recording_from, comparison, name1, name2, and repeat_num
    columns. Can also take prep_date, fly_num, thorimage_id.
    """
    missing_repeats_df = missing_repeats(df, n_repeats=n_repeats)
    if len(missing_repeats_df) == 0:
        return True
    else:
        return False


def missing_odor_pairs(df):
    """
    Requires at least recording_from, comparison, name1, name2 columns.
    Can also take prep_date, fly_num, thorimage_id.
    """
    # TODO check that for each comparison, both A, B, and A+B are there
    # (3 combos of name1, name2, or whichever other odor ids)
    comp_cols = []
    opt_rec_cols = [
        'prep_date',
        'fly_num',
        'thorimage_id'
    ]
    for oc in opt_rec_cols:
        if oc in df.columns:
            comp_cols.append(oc)

    comp_cols += [
        'recording_from',
        'comparison'
    ]

    odor_cols = [
        'name1',
        'name2'
    ]

    incomplete_comparison_dfs = []
    for g, gdf in df.groupby(comp_cols):
        comp_odor_pairs = gdf[odor_cols].drop_duplicates()
        if len(comp_odor_pairs) != 3:
            incomplete_comparison_dfs.append(gdf[comp_cols].drop_duplicates(
                ).reset_index(drop=True))

        # TODO generate expected combinations of name1,name2
        # TODO possible either odor not in db, in which case, would need extra
        # information to say which odor is actually missing... (would need
        # stimulus data)
        '''
        if len(missing_odor_pairs) > 0:
            gmeta = gdf[comp_cols].drop_duplicates().reset_index(drop=True)

        for r in missing_odor_pairs:
            new_row = gmeta.copy()
            new_row['repeat_num'] = r
            missing_odor_pair_dfs.append(new_row)
        '''

    if len(incomplete_comparison_dfs) == 0:
        incomplete_comparison_df = pd.DataFrame({r: [] for r in comp_cols})
    else:
        incomplete_comparison_df = \
            pd.concat(incomplete_comparison_dfs, ignore_index=True)

    incomplete_comparison_df.recording_from = \
        pd.to_datetime(incomplete_comparison_df.recording_from)

    return incomplete_comparison_df


def have_full_comparisons(df):
    """
    Requires at least recording_from, comparison, name1, name2 columns.
    Can also take prep_date, fly_num, thorimage_id.
    """
    # TODO docstring
    if len(missing_odor_pairs(df)) == 0:
        return True
    else:
        return False


def skipped_comparison_nums(df):
    # TODO doc
    """
    Requires at least recording_from and comparison columns.
    Can also take prep_date, fly_num, and thorimage_id.
    """
    rec_cols = []
    opt_rec_cols = [
        'prep_date',
        'fly_num',
        'thorimage_id'
    ]
    for oc in opt_rec_cols:
        if oc in df.columns:
            rec_cols.append(oc)

    rec_cols.append('recording_from')

    skipped_comparison_dfs = []
    for g, gdf in df.groupby(rec_cols):
        max_comp_num = gdf.comparison.max()
        min_comp_num = gdf.comparison.min()
        skipped_comp_nums = [x for x in range(min_comp_num, max_comp_num + 1)
            if x not in gdf.comparison]

        if len(skipped_comp_nums) > 0:
            gmeta = gdf[rec_cols].drop_duplicates().reset_index(drop=True)

        for c in skipped_comp_nums:
            new_row = gmeta.copy()
            new_row['comparison'] = c
            skipped_comparison_dfs.append(new_row)

    if len(skipped_comparison_dfs) == 0:
        skipped_comparison_df = pd.DataFrame({r: [] for r in
            rec_cols + ['comparison']})
    else:
        skipped_comparison_df = \
            pd.concat(skipped_comparison_dfs, ignore_index=True)

    # TODO move this out of each of these check fns, and put wherever this
    # columns is generated (in the way that required this cast...)
    skipped_comparison_df.recording_from = \
        pd.to_datetime(skipped_comparison_df.recording_from)

    return skipped_comparison_df


def no_skipped_comparisons(df):
    # TODO doc
    """
    Requires at least recording_from and comparison columns.
    Can also take prep_date, fly_num, and thorimage_id.
    """
    if len(skipped_comparison_nums(df)) == 0:
        return True
    else:
        return False


# TODO do i actually need this, or just call drop_orphaned/missing_...?
'''
def db_has_all_repeats():
    # TODO just read db and call have_all_repeats
    # TODO may need to merge stuff?
    raise NotImplementedError
'''


# TODO also check recording has as many blocks (in df / in db) as it's supposed
# to, given what the metadata + gsheet say


def drop_orphaned_presentations():
    # TODO only stuff that isn't also most recent response params?
    # TODO find presentation rows that don't have response row referring to them
    raise NotImplementedError


# TODO TODO maybe implement check fns above as wrappers around another fn that
# finds inomplete stuff? (check if len is 0), so that these fns can just wrap
# the same thing...
def drop_incomplete_presentations():
    raise NotImplementedError


def smooth(x, window_len=11, window='hanning'):
    """smooth the data using a window with requested size.
    
    This method is based on the convolution of a scaled window with the signal.
    The signal is prepared by introducing reflected copies of the signal 
    (with the window size) in both ends so that transient parts are minimized
    in the begining and end part of the output signal.
    
    input:
        x: the input signal 
        window_len: the dimension of the smoothing window; should be an odd
            integer
        window: the type of window from 'flat', 'hanning', 'hamming',
            'bartlett', 'blackman' flat window will produce a moving average
            smoothing.

    output:
        the smoothed signal
        
    example:

    t=linspace(-2,2,0.1)
    x=sin(t)+randn(len(t))*0.1
    y=smooth(x)
    
    see also: 
    
    numpy.hanning, numpy.hamming, numpy.bartlett, numpy.blackman, numpy.convolve
    scipy.signal.lfilter
 
    TODO: the window parameter could be the window itself if an array instead of
    a string
    NOTE: length(output) != length(input), to correct this: return
    y[(window_len/2-1):-(window_len/2)] instead of just y.
    """
    if x.ndim != 1:
        raise ValueError("smooth only accepts 1 dimension arrays.")

    if x.size < window_len:
        raise ValueError("Input vector needs to be bigger than window size.")


    if window_len<3:
        return x


    if not window in ['flat', 'hanning', 'hamming', 'bartlett', 'blackman']:
        raise ValueError("Window is on of 'flat', 'hanning', " +
            "'hamming', 'bartlett', 'blackman'")


    # is this necessary?
    #s = np.r_[x[window_len-1:0:-1],x,x[-2:-window_len-1:-1]]

    #print(len(s))
    if window == 'flat': #moving average
        w = np.ones(window_len, 'd')
    else:
        w = eval('np.' + window + '(window_len)')

    #y = np.convolve(w/w.sum(), s, mode='valid')
    # not sure what to change above to get this to work...

    y = np.convolve(w/w.sum(), x, mode='same')
    return y


# TODO finish translating. was directly translating matlab registration script
# to python.
"""
def motion_correct_to_tiffs(thorimage_dir, output_dir):
    # TODO only read this if at least one motion correction would be run
    movie = read_movie(thorimage_dir)

    # TODO do i really want to basically just copy the matlab version?
    # opportunity for some refactoring?

    output_subdir = 'tif_stacks'

    _, thorimage_id = split(thorimage_dir)

    rig_tif = join(output_dir, output_subdir, thorimage_id + '_rig.tif')
    avg_rig_tif = join(output_dir, output_subdir, 'AVG', 'rigid',
        'AVG{}_rig.tif'.format(thorimage_id))

    nr_tif = join(output_dir, output_subdir, thorimage_id + '_nr.tif')
    avg_nr_tif = join(output_dir, output_subdir, 'AVG', 'nonrigid',
        'AVG{}_nr.tif'.format(thorimage_id))

    need_rig_tif = not exist(rig_tif)
    need_avg_rig_tif = not exist(avg_rig_tif)
    need_nr_tif = not exist(nr_tif)
    need_avg_nr_tif = not exist(avg_nr_tif)

    if not (need_rig_tif or need_avg_rig_tif or need_nr_tif or need_avg_nr_tif):
        print('All registration already done.')
        return

    # Remy: this seems like it might just be reading in the first frame?
    ###Y = input_tif_path
    # TODO maybe can just directly use filename for python version though? raw
    # even?

    # rigid moco (normcorre)
    # TODO just pass filename instead of Y, and compute dimensions or whatever
    # separately, so that normcorre can (hopefully?) take up less memory
    if need_rig_tif:
        MC_rigid = MotionCorrection(Y)

        options_rigid = NoRMCorreSetParms('d1',MC_rigid.dims(1),
            'd2',MC_rigid.dims(2),
            'bin_width',50,
            'max_shift',15,
            'phase_flag', 1,
            'us_fac', 50,
            'init_batch', 100,
            'plot_flag', false,
            'iter', 2) 

        # TODO so is nothing actually happening in parallel?
        ## rigid moco
        MC_rigid.motionCorrectSerial(options_rigid)  # can also try parallel
        # TODO which (if any) of these do i still want?
        MC_rigid.computeMean()
        MC_rigid.correlationMean()
        #####MC_rigid.crispness()
        print('normcorre done')

        ## plot shifts
        #plt.plot(MC_rigid.shifts_x)
        #plt.plot(MC_rigid.shifts_y)

        # save .tif
        M = MC_rigid.M
        M = uint16(M) 
        tiffoptions.overwrite = true

        print(['saving tiff to ' rig_tif])
        saveastiff(M, rig_tif, tiffoptions)

    if need_avg_rig_tif:
        ##
        # save average image
        #AVG = single(mean(MC_rigid.M,3))
        AVG = single(MC_rigid.template)
        tiffoptions.overwrite = true

        print(['saving tiff to ' avg_rig_tif])
        saveastiff(AVG, avg_rig_tif, tiffoptions)

    if need_nr_tif:
        MC_nonrigid = MotionCorrection(Y)
        options_nonrigid = NoRMCorreSetParms('d1',MC_nonrigid.dims(1),
            'd2',MC_nonrigid.dims(2),
            'grid_size',[64,64],
            'mot_uf',4,
            'bin_width',50,
            'max_shift',[15 15],
            'max_dev',3,
            'us_fac',50,
            'init_batch',200,
            'iter', 2)

        MC_nonrigid.motionCorrectParallel(options_nonrigid)
        MC_nonrigid.computeMean()
        MC_nonrigid.correlationMean()
        MC_nonrigid.crispness()
        print('non-rigid normcorre done')

        # save .tif
        M = uint16(MC_nonrigid.M)
        tiffoptions.overwrite  = true
        print(['saving tiff to ' nr_tif])
        saveastiff(M, nr_tif, tiffoptions)

    if need_avg_nr_tif:
        # TODO flag to disable saving this average
        #AVG = single(mean(MC_nonrigid.M,3))
        AVG = single(MC_nonrigid.template)
        tiffoptions.overwrite = true
        print(['saving tiff to ' avg_nr_tif])
        saveastiff(AVG, avg_nr_tif, tiffoptions)

    raise NotImplementedError
"""

def cell_ids(df):
    """Takes a DataFrame with 'cell' in MultiIndex or columns to unique values.
    """
    if 'cell' in df.index.names:
        return df.index.get_level_values('cell').unique().to_series()
    elif 'cell' in df.columns:
        cids = pd.Series(data=df['cell'].unique(), name='cell')
        cids.index.name = 'cell'
        return cids
    else:
        raise ValueError("'cell' not in index or columns of DataFrame")


def matlabels(df, rowlabel_fn):
    """
    Takes DataFrame and function that takes one row of index to a label.

    `rowlabel_fn` should take a DataFrame row (w/ columns from index) to a str.
    """
    return df.index.to_frame().apply(rowlabel_fn, axis=1)


def format_odor_conc(name, log10_conc):
    """Takes `str` odor name and log10 concentration to a formatted `str`.
    """
    if log10_conc is None:
        return name
    else:
        # TODO tex formatting for exponent
        #return r'{} @ $10^{{'.format(name) + '{:.2f}}}$'.format(log10_conc)
        return '{} @ $10^{{{:.2f}}}$'.format(name, log10_conc)


def format_mixture(*args):
    """Returns `str` representing 2-component odor mixture.

    Input can be any of:
    - 2 `str` names
    - 2 names and concs (n1, n2, c1, c2)
    - a pandas.Series / dict with keys `name1`, `name2`, and (optionally)
      `log10_concvv<1/2>`
    """
    log10_c1 = None
    log10_c2 = None
    if len(args) == 2:
        n1, n2 = args
    elif len(args) == 4:
        n1, n2, log10_c1, log10_c2 = args
    elif len(args) == 1:
        row = args[0]
        n1 = row['name1']
        try:
            n2 = row['name2']
        except KeyError:
            n2 = None
        if 'log10_conc_vv1' in row:
            log10_c1 = row['log10_conc_vv1']
            if n2 is not None:
                log10_c2 = row['log10_conc_vv2']
    else:
        raise ValueError('incorrect number of args')

    if n1 == 'paraffin':
        title = format_odor_conc(n2, log10_c2)
    elif n2 == 'paraffin' or n2 == 'no_second_odor' or n2 is None:
        title = format_odor_conc(n1, log10_c1)
    else:
        title = '{} + {}'.format(
            format_odor_conc(n1, log10_c1),
            format_odor_conc(n2, log10_c2)
        )

    return title


def format_keys(date, fly, *other_keys):
    date = date.strftime(date_fmt_str)
    fly = str(int(fly))
    others = [str(k) for k in other_keys]
    return '/'.join([date] + [fly] + others)


# TODO rename to be inclusive of cases other than pairs
def pair_ordering(comparison_df):
    """Takes a df w/ name1 & name2 to a dict of their tuples to order int.
    """
    # TODO maybe assert only 3 combinations of name1/name2
    pairs = [(x.name1, x.name2) for x in
        comparison_df[['name1','name2']].drop_duplicates().itertuples()]

    # Will define the order in which odor pairs will appear, left-to-right,
    # in subplots.
    ordering = dict()

    has_paraffin = [p for p in pairs if 'paraffin' in p]
    if len(has_paraffin) == 0:
        assert {x[1] for x in pairs} == {'no_second_odor'}
        odors = [p[0] for p in pairs]

        # TODO TODO also support case where there isn't something we want to
        # stick at the end like this, for Matt's case
        last = None
        for o in odors:
            lo = o.lower()
            if 'approx' in lo or 'mix' in lo:
                if last is None:
                    last = o
                else:
                    raise ValueError('multiple mixtures in odors to order')
        ordering[(last, 'no_second_odor')] = len(odors) - 1
        
        i = 0
        for o in sorted(odors):
            if o == last:
                continue
            ordering[(o, 'no_second_odor')] = i
            i += 1
    else:
        no_pfo = [p for p in pairs if 'paraffin' not in p]
        if len(no_pfo) < 1:
            raise ValueError('All pairs for this comparison had paraffin.' +
                ' Analysis error? Incomplete recording?')

        assert len(no_pfo) == 1
        last = no_pfo[0]
        ordering[last] = 2

        for i, p in enumerate(sorted(has_paraffin,
            key=lambda x: x[0] if x[1] == 'paraffin' else x[1])):

            ordering[p] = i

    return ordering


def matshow(df, title=None, ticklabels=None, xticklabels=None,
    yticklabels=None, xtickrotation=None, colorbar_label=None,
    group_ticklabels=False, ax=None, fontsize=None, fontweight=None):
    # TODO shouldn't this get ticklabels from matrix if nothing else?
    # maybe at least in the case when both columns and row indices are all just
    # one level of strings?

    made_fig = False
    if ax is None:
        fig = plt.figure()
        ax = fig.add_subplot(111)
        made_fig = True

    def one_level_str_index(index):
        return (len(index.shape) == 1 and
            all(index.map(lambda x: type(x) is str)))

    if (xticklabels is None) and (yticklabels is None):
        if ticklabels is None:
            if one_level_str_index(df.columns):
                xticklabels = df.columns
            else:
                xticklabels = None
            if one_level_str_index(df.index):
                yticklabels = df.index
            else:
                yticklabels = None
        else:
            assert df.shape[0] == df.shape[1]
            # TODO maybe also assert indices are actually equal?
            xticklabels = ticklabels
            yticklabels = ticklabels
    else:
        # TODO delete this hack
        pass

    # TODO update this formula to work w/ gui corrs (too big now)
    if fontsize is None:
        fontsize = min(10.0, 240.0 / max(df.shape[0], df.shape[1]))

    cax = ax.matshow(df)

    # just doing it in this case now to support kc_analysis use case
    # TODO put behind flag or something
    if made_fig:
        cbar = fig.colorbar(cax)

        if colorbar_label is not None:
            # rotation=270?
            cbar.ax.set_ylabel(colorbar_label)

        # TODO possible to provide facilities for colorbar in case when ax is
        # passed in? pass in another ax for colorbar? or just as easy to handle
        # outside in that case (probably)?

    def grouped_labels_info(labels):
        if not group_ticklabels or labels is None:
            return labels, 1, 0

        labels = pd.Series(labels)
        n_repeats = int(len(labels) / len(labels.unique()))

        # TODO TODO assert same # things from each unique element.
        # that's what this whole tickstep thing seems to assume.

        # Assumes order is preserved if labels are grouped at input.
        # May need to calculate some other way if not always true.
        labels = labels.unique()
        tick_step = n_repeats
        offset = n_repeats / 2 - 0.5
        return labels, tick_step, offset

    # TODO automatically only group labels in case where all repeats are
    # adjacent?
    # TODO make fontsize / weight more in group_ticklabels case?
    xticklabels, xstep, xoffset = grouped_labels_info(xticklabels)
    yticklabels, ystep, yoffset = grouped_labels_info(yticklabels)

    if xticklabels is not None:
        # TODO nan / None value aren't supported in ticklabels are they?
        # (couldn't assume len is defined if so)
        if xtickrotation is None:
            if all([len(x) == 1 for x in xticklabels]):
                xtickrotation = 'horizontal'
            else:
                xtickrotation = 'vertical'

        ax.set_xticklabels(xticklabels, fontsize=fontsize,
            fontweight=fontweight, rotation=xtickrotation)
        #    rotation='horizontal' if group_ticklabels else 'vertical')
        ax.set_xticks(np.arange(0, len(df.columns), xstep) + xoffset)

    if yticklabels is not None:
        ax.set_yticklabels(yticklabels, fontsize=fontsize,
            fontweight=fontweight, rotation='horizontal')
        #    rotation='vertical' if group_ticklabels else 'horizontal')
        ax.set_yticks(np.arange(0, len(df), ystep) + yoffset)

    # TODO test this doesn't change rotation if we just set rotation above

    # this doesn't seem like it will work, since it seems to clear the default
    # ticklabels that there actually were...
    #ax.set_yticklabels(ax.get_yticklabels(), fontsize=fontsize,
    #    fontweight=fontweight)

    # didn't seem to do what i was expecting
    #ax.spines['bottom'].set_visible(False)
    ax.tick_params(bottom=False)

    if title is not None:
        ax.set_xlabel(title, fontsize=(fontsize + 1.5), labelpad=12)

    if made_fig:
        plt.tight_layout()
        return fig
    else:
        return cax


# TODO maybe one fn that puts in matrix format and another in table
# (since matrix may be sparse...)
def plot_pair_n(df, *args):
    """Plots a matrix of odor1 X odor2 w/ counts of flies as entries.

    Args:
    df (pd.DataFrame): DataFrame with columns:
        - prep_date
        - fly_num
        - thorimage_id
        - name1
        - name2
        Data already collected w/ odor pairs.

    odor_panel (pd.DataFrame): (optional) DataFrame with columns:
        - odor_1
        - odor_2
        - reason (maybe make this optional?)
        The odor pairs experiments are supposed to collect data for.
    """
    import imgkit
    import seaborn as sns

    odor_panel = None
    if len(args) == 1:
        odor_panel = args[0]
    elif len(args) != 0:
        raise ValueError('incorrect number of arguments')
    # TODO maybe make df optional and read from database if it's not passed?
    # TODO a flag to show all stuff marked attempt analysis in gsheet?

    # TODO borrow more of this / call this in part of kc_analysis that made that
    # table w/ these counts for repeats?

    # TODO also handle no_second_odor
    df = df.drop(
        index=df[(df.name1 == 'paraffin') | (df.name2 == 'paraffin')].index)

    # TODO possible to do at least a partial check w/ n_accepted_blocks sum?
    # (would have to do outside of this fn. presentations here doesn't have it.
    # whatever latest_analysis returns might.)

    replicates = df[
        ['prep_date','fly_num','recording_from','name1','name2']
    ].drop_duplicates()

    # TODO do i actually want margins? (would currently count single odors twice
    # if in multiple comparison... may at least not want that?)
    # hide margins for now.
    pair_n = pd.crosstab(replicates.name1, replicates.name2) #, margins=True)

    # Making the rectangular matrix pair_n square
    # (same indexes on row and column axes)

    if odor_panel is None:
        # This is basically equivalent to the logic in the branch below,
        # although the index is not defined separately here.
        full_pair_n = pair_n.combine_first(pair_n.T).fillna(0.0)
    else:
        # TODO [change odor<n> to / handle] name<n>, to be consistent w/ above
        # TODO TODO TODO also make this triangular / symmetric
        odor_panel = odor_panel.pivot_table(index='odor_1', columns='odor_2',
            aggfunc=lambda x: True, values='reason')

        full_panel_index = odor_panel.index.union(odor_panel.columns)
        full_data_index = pair_n.index.union(pair_n.columns)
        assert full_data_index.isin(full_panel_index).all()
        # TODO also check no pairs occur in data that are not in panel
        # TODO isin-like check for tuples (or other combinations of rows)?
        # just iterate over both after drop_duplicates?

        full_pair_n = pair_n.reindex(index=full_panel_index
            ).reindex(columns=full_panel_index)
        # TODO maybe making symmetric is a matter of setting 0 to nan here?
        # and maybe setting back to nan at the end if still 0?
        full_pair_n.update(full_pair_n.T)
        # TODO full_pair_n.fillna(0, inplace=True)?

    # TODO TODO delete this hack once i find a nicer way to make the
    # output of crosstab symmetric
    for i in range(full_pair_n.shape[0]):
        for j in range(full_pair_n.shape[1]):
            a = full_pair_n.iat[i,j]
            b = full_pair_n.iat[j,i]
            if a > 0 and (pd.isnull(b) or b == 0):
                full_pair_n.iat[j,i] = a
            elif b > 0 and (pd.isnull(a) or a == 0):
                full_pair_n.iat[i,j] = b
    # TODO also delete this hack. this assumes that anything set to 0
    # is not actually a pair in the panel (which should be true right now
    # but will not always be)
    full_pair_n.replace(0, np.nan, inplace=True)
    #

    # TODO TODO TODO make crosstab output actually symmetric, not just square
    # (or is it always one diagonal that's filled in? if so, really just need
    # that)
    assert full_pair_n.equals(full_pair_n.T)

    # TODO TODO TODO how to indicate which of the pairs we are actually
    # interested in? grey out the others? white the others? (just set to nan?)
    # (maybe only use to grey / white out if passed in?)
    # (+ margins for now)

    # TODO TODO TODO color code text labels by pair selection reason + key
    # TODO what to do when one thing falls under two reasons though...?
    # just like a key (or things alongside ticklabels) that has each color
    # separately? just symbols in text, if that's easier?

    # TODO TODO display actual counts in squares in matshow
    # maybe make colorbar have discrete steps?

    full_pair_n.fillna('', inplace=True)
    cm = sns.light_palette('seagreen', as_cmap=True)
    # TODO TODO if i'm going to continue using styler + imgkit
    # at least figure out how to get the cmap to actually work
    # need some css or something?
    html = full_pair_n.style.background_gradient(cmap=cm).render()
    imgkit.from_string(html, 'natural_odors_pair_n.png')


# TODO test when ax actually is passed in now that I made it a kwarg
# (also works as optional positional arg, right?)
def closed_mpl_contours(footprint, ax=None, if_multiple='err', **kwargs):
    """
    Args:
        if_multiple (str): 'take_largest'|'join'|'err'
    """
    dims = footprint.shape
    padded_footprint = np.zeros(tuple(d + 2 for d in dims))
    padded_footprint[tuple(slice(1,-1) for _ in dims)] = footprint
    
    # TODO delete
    #fig = plt.figure()
    #
    if ax is None:
        ax = plt.gca()

    mpl_contour = ax.contour(padded_footprint > 0, [0.5], **kwargs)
    # TODO which of these is actually > 1 in multiple comps case?
    # handle that one approp w/ err_on_multiple_comps!
    assert len(mpl_contour.collections) == 1

    paths = mpl_contour.collections[0].get_paths()

    if len(paths) != 1:
        if if_multiple == 'err':
            raise RuntimeError('multiple disconnected paths in one footprint')

        elif if_multiple == 'take_largest':
            largest_sum = 0
            largest_idx = 0
            total_sum = 0
            for p in range(len(paths)):
                path = paths[p]

                # TODO TODO TODO maybe replace mpl stuff w/ cv2 drawContours?
                # (or related...) (fn now in here as contour2mask)
                mask = np.ones_like(footprint, dtype=bool)
                for x, y in np.ndindex(footprint.shape):
                    # TODO TODO not sure why this seems to be transposed, but it
                    # does (make sure i'm not doing something wrong?)
                    if path.contains_point((x, y)):
                        mask[x, y] = False
                # Places where the mask is False are included in the sum.
                path_sum = MaskedArray(footprint, mask=mask).sum()
                # TODO maybe check that sum of all path_sums == footprint.sum()?
                # seemed there were some paths w/ 0 sum... cnmf err?
                '''
                print('mask_sum:', (~ mask).sum())
                print('path_sum:', path_sum)
                print('regularly masked sum:', footprint[(~ mask)].sum())
                plt.figure()
                plt.imshow(mask)
                plt.figure()
                plt.imshow(footprint)
                plt.show()
                import ipdb; ipdb.set_trace()
                '''
                if path_sum > largest_sum:
                    largest_sum = path_sum
                    largest_idx = p

                total_sum += path_sum
            footprint_sum = footprint.sum()
            # TODO float formatting / some explanation as to what this is
            print('footprint_sum:', footprint_sum)
            print('total_sum:', total_sum)
            print('largest_sum:', largest_sum)
            # TODO is this only failing when stuff is overlapping?
            # just merge in that case? (wouldn't even need to dilate or
            # anything...) (though i guess then the inequality would go the
            # other way... is it border pixels? just ~dilate by one?)
            # TODO fix + uncomment
            ######assert np.isclose(total_sum, footprint_sum)
            path = paths[largest_idx]

        elif if_multiple == 'join':
            raise NotImplementedError
    else:
        path = paths[0]

    # TODO delete
    #plt.close(fig)
    #

    contour = path.vertices
    # Correct index change caused by padding.
    return contour - 1


def plot_traces(*args, footprints=None, order_by='odors', scale_within='cell',
    n=20, random=False, title=None, response_calls=None, raw=False,
    smoothed=True, show_footprints=True, show_footprints_alone=False,
    show_cell_ids=True, show_footprint_with_mask=False, gridspec=None,
    linewidth=0.5, verbose=True):
    # TODO TODO be clear on requirements of df and cell_ids in docstring
    """
    n (int): (default=20) Number of cells to plot traces for if cell_ids not
        passed as second positional argument.
    random (bool): (default=False) Whether the `n` cell ids should be selected
        randomly. If False, the first `n` cells are used.
    order_by (str): 'odors' or 'presentation_order'
    scale_within (str): 'none', 'cell', or 'trial'
    gridspec (None or matplotlib.gridspec.*): region of a parent figure
        to draw this plot on.
    linewidth (float): 0.25 seemed ok on CNMF data, but too small w/ clean
    traces.
    """
    import tifffile
    import cv2
    # TODO maybe use cv2 and get rid of this dep?
    from skimage import color

    # TODO make text size and the spacing of everything more invariant to figure
    # size. i think the default size of this figure ended up being bigger when i
    # was using it in kc_analysis than it is now in the gui, so it isn't display
    # well in the gui, but fixing it here might break it in the kc_analysis case
    if verbose:
        print('Entering plot_traces...')

    if len(args) == 1:
        df = args[0]
        # TODO flag to also subset to responders first?
        all_cells = cell_ids(df)
        n = min(n, len(all_cells))
        if random:
            # TODO maybe flag to disable seed?
            cells = all_cells.sample(n=n, random_state=1)
        else:
            cells = all_cells[:n]

    elif len(args) == 2:
        df, cells = args

    else:
        raise ValueError('must call with either df or df and cells')

    if show_footprints:
        # or maybe just download (just the required!) footprints from sql?
        if footprints is None:
            raise ValueError('must pass footprints kwarg if show_footprints')
        # decide whether this should be in the preconditions or just done here
        # (any harm to just do here anyway?)
        #else:
        #    footprints = footprints.set_index(recording_cols + ['cell'])

    # TODO TODO TODO fix odor labels as in matrix (this already done?)
    # (either rotate or use abbreviations so they don't overlap!)

    # TODO check order_by and scale_within are correct
    assert raw or smoothed

    # TODO maybe automatically show_cells if show_footprints is true,
    # otherwise don't?
    # TODO TODO maybe indicate somehow the multiple response criteria
    # when it is a list (add border & color each half accordingly?)

    extra_cols = 0
    # TODO which of these cases do i want to support here?
    if show_footprints:
        if show_footprints_alone:
            extra_cols = 2
        else:
            extra_cols = 1
    elif show_footprints_alone:
        raise NotImplementedError

    # TODO possibility of other column for avg + roi overlays
    # possible to make it larger, or should i use a layout other than
    # gridspec? just give it more grid elements?
    # TODO for combinatorial combinations of flags enabling cols on
    # right, maybe set index for each of those flags up here

    # TODO could also just could # trials w/ drop_duplicates, for more
    # generality
    n_repeats = n_expected_repeats(df)
    n_trials = n_repeats * len(df[['name1','name2']].drop_duplicates())

    if gridspec is None:
        # This seems to hang... not sure if it's usable w/ some changes.
        #fig = plt.figure(constrained_layout=True)
        fig = plt.figure()
        gs = fig.add_gridspec(4, 6, hspace=0.4, wspace=0.3)
        made_fig = True
    else:
        fig = gridspec.get_topmost_subplotspec().get_gridspec().figure
        gs = gridspec.subgridspec(4, 6, hspace=0.4, wspace=0.3)
        made_fig = False

    if show_footprints:
        trace_gs_slice = gs[:3,:4]
    else:
        trace_gs_slice = gs[:,:]

    # For common X/Y labels
    bax = fig.add_subplot(trace_gs_slice, frameon=False)
    # hide tick and tick label of the big axes
    bax.tick_params(top=False, bottom=False, left=False, right=False,
        labelcolor='none')
    bax.grid(False)

    trace_gs = trace_gs_slice.subgridspec(len(cells),
        n_trials + extra_cols, hspace=0.15, wspace=0.06)

    axs = []
    for ti in range(trace_gs._nrows):
        axs.append([])
        for tj in range(trace_gs._ncols):
            ax = fig.add_subplot(trace_gs[ti,tj])
            axs[-1].append(ax)
    axs = np.array(axs)

    # TODO want all of these behind show_footprints?
    if show_footprints:
        # TODO use 2/3 for widgets?
        # TODO or just text saying which keys to press? (if only
        # selection mechanism is going to be hover, mouse clicks
        # wouldn't make sense...)

        avg_ax = fig.add_subplot(gs[:, -2:])
        # TODO TODO maybe show trial movie beneath this?
        # (also on hover/click like (trial,cell) data)

    if title is not None:
        #pad = 40
        pad = 15
        # was also using default fontsize here in kc_analysis use case
        # increment pad by 5 for each newline in title?
        bax.set_title(title, pad=pad, fontsize=9)

    bax.set_ylabel('Cell')

    # This pad is to make it not overlap w/ time label on example plot.
    # Was left to default value for kc_analysis.
    # TODO negative labelpad work? might get drawn over by axes?
    labelpad = -10
    if order_by == 'odors':
        bax.set_xlabel('Trials ordered by odor', labelpad=labelpad)
    elif order_by == 'presentation_order':
        bax.set_xlabel('Trials in presentation order', labelpad=labelpad)

    ordering = pair_ordering(df)

    '''
    display_start_time = -3.0
    display_stop_time = 10
    display_window = df[
        (comparison_df.from_onset >= display_start_time) &
        (comparison_df.from_onset <= display_stop_time)]
    '''
    display_window = df

    smoothing_window_secs = 1.0
    fps = fps_from_thor(df)
    window_size = int(np.round(smoothing_window_secs * fps))

    group_cols = trial_cols + ['order']

    xmargin = 1
    xmin = display_window.from_onset.min() - xmargin
    xmax = display_window.from_onset.max() + xmargin

    response_rgb = (0.0, 1.0, 0.2)
    nonresponse_rgb = (1.0, 0.0, 0.0)
    response_call_alpha = 0.2

    if scale_within == 'none':
        ymin = None
        ymax = None

    cell2contour = dict()
    cell2rect = dict()
    cell2text_and_rect = dict()

    seen_ij = set()
    avg = None
    for i, cell_id in enumerate(cells):
        if verbose:
            print('Plotting cell {}/{}...'.format(i + 1, len(cells)))

        cell_data = display_window[display_window.cell == cell_id]
        cell_trials = cell_data.groupby(group_cols, sort=False)[
            ['from_onset','df_over_f']]

        prep_date = pd.Timestamp(cell_data.prep_date.unique()[0])
        date_dir = prep_date.strftime(date_fmt_str)
        fly_num = cell_data.fly_num.unique()[0]
        thorimage_id = cell_data.thorimage_id.unique()[0]

        #assert len(cell_trials) == axs.shape[1]

        if show_footprints:
            if avg is None:
                # only uncomment to support dff images and other stuff like that
                '''
                try:
                    # TODO either put in docstring that datetime.datetime is
                    # required, or cast input date as appropriate
                    # (does pandas date type support strftime?)
                    # or just pass date_dir?
                    # TODO TODO should not use nr if going to end up using the
                    # rig avg... but maybe lean towards computing the avg in
                    # that case rather than deferring to rigid?
                    tif = motion_corrected_tiff_filename(
                        prep_date, fly_num, thorimage_id)
                except IOError as e:
                    if verbose:
                        print(e)
                    continue

                # TODO maybe show progress bar / notify on this step
                if verbose:
                    print('Loading full movie from {} ...'.format(tif),
                        end='', flush=True)
                movie = tifffile.imread(tif)
                if verbose:
                    print(' done.')
                '''

                # TODO modify motion_corrected_tiff_filename to work in this
                # case too?
                tif_dir = join(analysis_output_root(), date_dir, str(fly_num),
                    'tif_stacks')
                avg_nr_tif = join(tif_dir, 'AVG', 'nonrigid',
                    'AVG{}_nr.tif'.format(thorimage_id))
                avg_rig_tif = join(tif_dir, 'AVG', 'rigid',
                    'AVG{}_rig.tif'.format(thorimage_id))

                avg_tif = None
                if exists(avg_nr_tif):
                    avg_tif = avg_nr_tif
                elif exists(avg_rig_tif):
                    avg_tif = avg_rig_tif

                if avg_tif is None:
                    raise IOError(('No average motion corrected TIFs ' +
                        'found in {}').format(tif_dir))

                avg = tifffile.imread(avg_tif)
                '''
                avg = cv2.normalize(avg, None, alpha=0, beta=1,
                    norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
                '''
                # TODO find a way to histogram equalize w/o converting
                # to 8 bit?
                avg = cv2.normalize(avg, None, alpha=0, beta=255,
                    norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8UC1)
                better_constrast = cv2.equalizeHist(avg)

                rgb_avg = color.gray2rgb(better_constrast)

            cell_row = (prep_date, fly_num, thorimage_id, cell_id)
            footprint_row = footprints.loc[cell_row]

            # TODO TODO TODO probably need to tranpose how footprint is handled
            # downstream (would prefer not to transpose footprint though)
            # (as i had to switch x_coords and y_coords in db as they were
            # initially entered swapped)
            footprint = db_row2footprint(footprint_row, shape=avg.shape)

            # TODO maybe some percentile / fixed size about maximum
            # density?
            cropped_footprint, ((x_min, x_max), (y_min, y_max)) = \
                crop_to_nonzero(footprint, margin=6)
            cell2rect[cell_id] = (x_min, x_max, y_min, y_max)

            cropped_avg = \
                better_constrast[x_min:x_max + 1, y_min:y_max + 1]

            if show_footprint_with_mask:
                # TODO figure out how to suppress clipping warning in the case
                # when it's just because of float imprecision (e.g. 1.0000001
                # being clipped to 1) maybe just normalize to [0 + epsilon, 1 -
                # epsilon]?
                # TODO TODO or just set one channel to be this
                # footprint?  scale first?
                cropped_footprint_rgb = \
                    color.gray2rgb(cropped_footprint)
                for c in (1,2):
                    cropped_footprint_rgb[:,:,c] = 0
                # TODO plot w/ value == 1 to test?

                cropped_footprint_hsv = \
                    color.rgb2hsv(cropped_footprint_rgb)

                cropped_avg_hsv = \
                    color.rgb2hsv(color.gray2rgb(cropped_avg))

                # TODO hue already seems to be constant at 0.0 (red?)
                # so maybe just directly set to red to avoid confusion?
                cropped_avg_hsv[..., 0] = cropped_footprint_hsv[..., 0]

                alpha = 0.3
                cropped_avg_hsv[..., 1] = cropped_footprint_hsv[..., 1] * alpha

                composite = color.hsv2rgb(cropped_avg_hsv)

                # TODO TODO not sure this is preserving hue/sat range to
                # indicate how strong part of filter is
                # TODO figure out / find some way that would
                # TODO TODO maybe don't normalize within each ROI? might
                # screw up stuff relative to histogram equalized
                # version...
                # TODO TODO TODO still normalize w/in crop in contour
                # case?
                composite = cv2.normalize(composite, None, alpha=0.0,
                    beta=1.0, norm_type=cv2.NORM_MINMAX,
                    dtype=cv2.CV_32F)

            else:
                # TODO could also use something more than this
                # TODO TODO fix bug here. see 20190402_bug1.txt
                # TODO TODO where are all zero footprints coming from?
                cropped_footprint_nonzero = cropped_footprint > 0
                if not np.any(cropped_footprint_nonzero):
                    continue

                level = \
                    cropped_footprint[cropped_footprint_nonzero].min()

            if show_footprints_alone:
                ax = axs[i,-2]
                f_ax = axs[i,-1]
                f_ax.imshow(cropped_footprint, cmap='gray')
                f_ax.axis('off')
            else:
                ax = axs[i,-1]

            if show_footprint_with_mask:
                ax.imshow(composite)
            else:
                ax.imshow(cropped_avg, cmap='gray')
                # TODO TODO also show any other contours in this rectangular ROI
                # in a diff color! (copy how gui does this)
                cell2contour[cell_id] = \
                    closed_mpl_contours(cropped_footprint, ax, colors='red')

            ax.axis('off')

            text = str(cell_id + 1)
            h = y_max - y_min
            w = x_max - x_min
            rect = patches.Rectangle((y_min, x_min), h, w,
                linewidth=1.5, edgecolor='b', facecolor='none')
            cell2text_and_rect[cell_id] = (text, rect)

        if scale_within == 'cell':
            ymin = None
            ymax = None

        for n, cell_trial in cell_trials:
            #(prep_date, fly_num, thorimage_id,
            (_, _, _, comp, o1, o2, repeat_num, order) = n

            # TODO TODO also support a 'fixed' order that B wanted
            # (which should also include missing stuff[, again in gray,]
            # ideally)
            if order_by == 'odors':
                j = n_repeats * ordering[(o1, o2)] + repeat_num

            elif order_by == 'presentation_order':
                j = order

            else:
                raise ValueError("supported orderings are 'odors' and "+
                    "'presentation_order'")

            if scale_within == 'trial':
                ymin = None
                ymax = None

            assert (i,j) not in seen_ij
            seen_ij.add((i,j))
            ax = axs[i,j]

            # So that events that get the axes can translate to cell /
            # trial information.
            ax.cell_id = cell_id
            ax.trial_info = n

            # X and Y axis major tick label fontsizes.
            # Was left to default for kc_analysis.
            ax.tick_params(labelsize=6)

            trial_times = cell_trial['from_onset']

            # TODO TODO why is *first* ea trial the one not shown, and
            # apparently the middle pfo trial
            # (was it not actually ordered by 'order'/frame_num outside of
            # odor order???)
            # TODO TODO TODO why did this not seem to work? (or only for
            # 1/3.  the middle one. iaa.)
            # (and actually title is still hidden for ea and pfo trials
            # mentioned above, but numbers / ticks / box still there)
            # (above notes only apply to odor order case. presentation order
            # worked)
            # TODO and why is gray title over correct axes in odor order case,
            # but axes not displaying data are in wrong place?
            # TODO is cell_trial messed up?

            # Supports at least the case when there are missing odor
            # presentations at the end of the ~block.
            missing_this_presentation = \
                trial_times.shape == (1,) and pd.isnull(trial_times.iat[0])

            if i == 0:
                # TODO group in odors case as w/ matshow?
                if order_by == 'odors':
                    trial_title = format_mixture({
                        'name1': o1,
                        'name2': o2,
                    })
                elif order_by == 'presentation_order':
                    trial_title = format_mixture({
                        'name1': o1,
                        'name2': o2
                    })

                if missing_this_presentation:
                    tc = 'gray'
                else:
                    tc = 'black'

                ax.set_title(trial_title, fontsize=6, color=tc)
                # TODO may also need to do tight_layout here...
                # it apply to these kinds of titles?

            if missing_this_presentation:
                ax.axis('off')
                continue

            trial_dff = cell_trial['df_over_f']

            if raw:
                if ymax is None:
                    ymax = trial_dff.max()
                    ymin = trial_dff.min()
                else:
                    ymax = max(ymax, trial_dff.max())
                    ymin = min(ymin, trial_dff.min())

                ax.plot(trial_times, trial_dff, linewidth=linewidth)

            if smoothed:
                # TODO kwarg(s) to control smoothing?
                sdff = smooth(trial_dff, window_len=window_size)

                if ymax is None:
                    ymax = sdff.max()
                    ymin = sdff.min()
                else:
                    ymax = max(ymax, sdff.max())
                    ymin = min(ymin, sdff.min())

                # TODO TODO have plot_traces take kwargs to be passed to
                # plotting fn + delete separate linewidth
                ax.plot(trial_times, sdff, color='black', linewidth=linewidth)

            # TODO also / separately subsample?

            if response_calls is not None:
                was_a_response = \
                    response_calls.loc[(o1, o2, repeat_num, cell_id)]

                if was_a_response:
                    ax.set_facecolor(response_rgb +
                        (response_call_alpha,))
                else:
                    ax.set_facecolor(nonresponse_rgb +
                        (response_call_alpha,))

            if i == axs.shape[0] - 1 and j == 0:
                # want these centered on example plot or across all?

                # I had not specified fontsize for kc_analysis case, so whatever
                # the default value was probably worked OK there.
                ax.set_xlabel('Seconds from odor onset', fontsize=6)

                if scale_within == 'none':
                    scaletext = ''
                elif scale_within == 'cell':
                    scaletext = '\nScaled within each cell'
                elif scale_within == 'trial':
                    scaletext = '\nScaled within each trial'

                # TODO just change to "% maximum w/in <x>" or something?
                # Was 70 for kc_analysis case. That's much too high here.
                #labelpad = 70
                labelpad = 10
                ax.set_ylabel(r'$\frac{\Delta F}{F}$' + scaletext,
                    rotation='horizontal', labelpad=labelpad)

                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                
            else:
                if show_cell_ids and j == len(cell_trials) - 1:
                    # Indexes as they would be from one. For comparison
                    # w/ Remy's MATLAB analysis.
                    # This and default fontsize worked for kc_analysis case,
                    # not for GUI.
                    #labelpad = 18
                    labelpad = 25
                    ax.set_ylabel(str(cell_id + 1),
                        rotation='horizontal', labelpad=labelpad, fontsize=5)
                    ax.yaxis.set_label_position('right')
                    # TODO put a label somewhere on the plot indicating
                    # these are cell IDs

                for d in ('top', 'right', 'bottom', 'left'):
                    ax.spines[d].set_visible(False)

                ax.set_xticks([])
                ax.set_yticks([])
                ax.set_xticklabels([])
                ax.set_yticklabels([])

        # TODO change units in this case on ylabel?
        # (to reflect how it was scaled)
        if scale_within == 'cell':
            for r in range(len(cell_trials)):
                ax = axs[i,r]
                ax.set_ylim(ymin, ymax)

    if scale_within == 'none':
        for i in range(len(cells)):
            for j in range(len(cell_trials)):
                ax = axs[i,j]
                ax.set_ylim(ymin, ymax)
    
    if show_footprints:
        fly_title = '{}, fly {}, {}'.format(
            date_dir, fly_num, thorimage_id)

        # title like 'Recording average fluorescence'?
        #avg_ax.set_title(fly_title)
        avg_ax.imshow(rgb_avg)
        avg_ax.axis('off')

        cell2rect_artists = dict()
        for cell_id in cells:
            # TODO TODO fix bug that required this (zero nonzero pixel
            # in cropped footprint thing...)
            if cell_id not in cell2text_and_rect:
                continue

            (text, rect) = cell2text_and_rect[cell_id]

            box = rect.get_bbox()
            # TODO appropriate font properties? placement still good?
            # This seemed to work be for (larger?) figures in kc_analysis,
            # too large + too close to boxes in gui (w/ ~8"x5" gridspec,dpi 100)
            # TODO set in relation to actual fig size (+ dpi?)
            #boxlabel_fontsize = 9
            boxlabel_fontsize = 6
            text_artist = avg_ax.text(box.xmin, box.ymin - 2, text,
                color='b', size=boxlabel_fontsize, fontweight='bold')
            # TODO jitter somehow (w/ arrow pointing to box?) to ensure no
            # overlap? (this would be ideal, but probably hard to implement)
            avg_ax.add_patch(rect)

            cell2rect_artists[cell_id] = (text_artist, rect)

    for i in range(len(cells)):
        for j in range(len(cell_trials)):
            ax = axs[i,j]
            ax.set_xlim(xmin, xmax)

    if made_fig:
        fig.tight_layout()
        return fig


def imshow(img, title):
    fig, ax = plt.subplots()
    ax.imshow(img, cmap='gray')
    ax.set_title(title)
    ax.axis('off')
    return fig


def image_grid(image_list):
    n = int(np.ceil(np.sqrt(len(image_list))))
    fig, axs = plt.subplots(n,n)
    for ax, img in zip(axs.flat, image_list):
        ax.imshow(img, cmap='gray')

    for ax in axs.flat:
        ax.axis('off')

    plt.subplots_adjust(wspace=0, hspace=0.05)
    return fig


def normed_u8(img):
    return (255 * (img / img.max())).astype(np.uint8)


def template_match(scene, template, method_str='cv2.TM_CCOEFF', hist=False):
    import cv2

    scene = normed_u8(scene)
    # TODO TODO maybe template should only be scaled to it's usual fraction of
    # max of the scene? like scaled both wrt orig_scene.max() / max across all
    # images?
    normed_template = normed_u8(template)

    method = eval(method_str)
    res = cv2.matchTemplate(scene, normed_template, method)

    # b/c for sqdiff[_normed], find minima. for others, maxima.
    if 'SQDIFF' in method_str:
        res = res * -1

    if hist:
        fh = plt.figure()
        plt.hist(res.flatten())
        plt.title('Matching output values ({})'.format(method_str))

    return res


def euclidean_dist(v1, v2):
    return np.linalg.norm(np.array(v1) - np.array(v2))


# TODO TODO TODO try updating to take max of two diff match images,
# created w/ different template scales (try a smaller one + existing),
# and pack appropriate size at each maxima.
# TODO make sure match criteria is comparable across scales (one threshold
# ideally) (possible? using one of normalized metrics sufficient? test this
# on fake test data?)
def greedy_roi_packing(match_image, radius, d, threshold=None, n=None, 
    exclusion_radius_frac=0.5, min_dist2neighbor=15, min_neighbors=3,
    draw_on=None, _claimed_from_double_radius=True,
    debug=False, bboxes=True, circles=True, nums=True):
    """
    Args:
    match_image (np.ndarray): 2-dimensional array of match value
        higher means better match of that point to template.

    radius (int): radius of cell in pixels.

    d (int): integer width (and height) of square template.
        related to radius, but differ by margin set outside.

    exclusion_radius_frac (float): approximately 1 - the fraction of two ROI
        radii that are allowed to overlap.

    """
    import cv2

    # TODO optimal non-greedy alg for this problem? (maximize weight of 
    # match_image summed across all assigned ROIs)

    if threshold is None and n is None:
        threshold = 0.8

    if not ((n is None and threshold is not None) or
            (n is not None and threshold is None)):
        raise ValueError('only specify either threshold or n')

    if draw_on is not None:
        # TODO figure out why background looks lighter here than in other 
        # imshows of avg

        draw_on = draw_on - np.min(draw_on)
        draw_on = draw_on / np.max(draw_on)
        cmap = plt.get_cmap('gray') #, lut=256)
        # (throwing away alpha coord w/ last slice)
        draw_on = np.round((cmap(draw_on)[:, :, :3] * 255)).astype(np.uint8)

        # upsampling just so cv2 drawing functions look better
        ups = 4
        draw_on = cv2.resize(draw_on,
            tuple([ups * x for x in draw_on.shape[:2]]))

    flat_vals = match_image.flatten()
    sorted_flat_indices = np.argsort(flat_vals)
    if n is None:
        idx = np.searchsorted(flat_vals[sorted_flat_indices], threshold)
        sorted_flat_indices = sorted_flat_indices[idx:]

    matches = np.unravel_index(sorted_flat_indices[::-1], match_image.shape)

    # TODO wait, why the minus 1?
    orig_shape = [x + d - 1 for x in match_image.shape]

    claimed = np.zeros(orig_shape, dtype=np.uint8)

    if _claimed_from_double_radius:
        # The factor of two is to test for would-be circle overlap by just
        # testing center point against mask painted with larger circles.
        exclusion_radius = int(round(2 * radius * exclusion_radius_frac))
    else:
        exclusion_radius = int(round(radius * exclusion_radius_frac))

    if debug:
        print('radius:', radius)
        print('exclusion_radius:', exclusion_radius)

    found_n = 0
    centers = []
    min_err = None
    for pt in zip(*matches[::-1]):
        if n is not None:
            if found_n >= n:
                break

        # TODO would some other (alternating?) rounding rule help?
        # TODO random seed then randomly choose between floor and ceil for stuff
        # at 0.5?
        offset = int(round(d / 2))
        center = (pt[0] + offset, pt[1] + offset)

        if _claimed_from_double_radius:
            if claimed[center[::-1]]:
                continue
        else:
            mask = np.zeros_like(claimed, dtype=np.uint8)
            cv2.circle(mask, center, exclusion_radius, 1, -1)
            assert mask.sum() > 0
            if np.any(claimed * mask):
                continue

        found_n += 1
        # TODO TODO was this just so output would be in imagej coords or
        # something? make cleaner.
        center = (center[0] - 1, center[1] - 1)
        centers.append(center)

        if draw_on is not None:
            draw_pt = (ups * pt[0], ups * pt[1])
            draw_c = (ups * center[0], ups * center[1])

            # TODO factor this stuff out into post-hoc drawing fn, so that
            # roi filters in here can exclude stuff? or maybe just factor out
            # the filtering stuff anyway?

            if bboxes:
                cv2.rectangle(draw_on, draw_pt,
                    (draw_pt[0] + ups * d, draw_pt[1] + ups * d), (0,0,255), 2)

            if circles:
                cv2.circle(draw_on, draw_c, ups * radius, (255,0,0), 2)

            if nums:
                cv2.putText(draw_on, str(found_n), draw_pt,
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0,255,0), 2)

        # TODO although it looked better in my one case i tested, is it correct
        # to only *not* adjust center in this case? maybe just adjust other
        # params. unit test edge cases.
        cv2.circle(claimed, (center[0] + 1, center[1] + 1), exclusion_radius,
            1, -1)

    #if debug:
    #    imshow(claimed, 'greedy_roi_packing claimed')

    if debug and draw_on is not None:
        imshow(draw_on, 'greedy_roi_packing debug')

    if not min_neighbors:
        filtered_centers = centers
    else:
        # TODO TODO maybe extend this to requiring the nth closest be closer
        # than a certain amount (to exclude 2 (or n) cells off by themselves)

        filtered_centers = []
        for i, center in enumerate(centers):
            n_neighbors = 0
            for j, other_center in enumerate(centers):
                if i == j:
                    continue

                dist = euclidean_dist(center, other_center)
                if dist <= min_dist2neighbor:
                    n_neighbors += 1

                if n_neighbors >= min_neighbors:
                    filtered_centers.append(center)
                    break

            if debug and n_neighbors < min_neighbors:
                print('filtering roi at', center,
                    'for lack of enough neighbors (had {})'.format(n_neighbors))

    # TODO would probably need to return radii too, if incorporating multi-scale
    # template matching w/ this
    return np.array(filtered_centers)


def autoroi_metadata_filename(ijroi_file):
    path, fname = split(ijroi_file)
    return join(path, '.{}.meta.p'.format(fname))


def fit_circle_rois(tif, template=None, margin=None, mean_cell_extent_um=None,
    avg=None, movie=None, method_str='cv2.TM_CCOEFF', threshold=2000,
    exclusion_radius_frac=0.6, min_neighbors=2, debug=False, write_ijrois=False,
    _force_write_to=None, max_cells_per_plane=650):
    """
    Even if movie or avg is passed in, tif is used to find metadata and
    determine where to save ImageJ ROIs.

    Returns centers, radius
    """
    import tifffile
    import cv2
    import ijroi

    required = [template, margin, mean_cell_extent_um]
    have_req = [a is not None for a in required]
    if any(have_req):
        if not all(have_req):
            req_str = ', '.join(['template', 'margin', 'mean_cell_extent_um'])
            raise ValueError(f'if any of ({req_str}) are passed, '
                'must pass them all')
    else:
        # TODO maybe options to cache this data across calls?
        # might not matter...
        data = template_data(err_if_missing=True)
        template = data['template']
        margin = data['margin']
        mean_cell_extent_um = data['mean_cell_extent_um']

    # TODO TODO maybe try incorporating slightly-varying-scale template
    # matching into greedy roi assignment procedure (normed corr useful there?)
    # TODO normed ccoeff equivalent to non-normed after appropriate choice of
    # threshold? (seemed earlier, maybe no, but i could have done the test
    # wrong)

    if write_ijrois or _force_write_to is not None:
        write_ijrois = True

        path, tiff_last_part = split(tif)
        tiff_parts = tiff_last_part.split('.tif')
        assert len(tiff_parts) == 2 and tiff_parts[1] == ''
        fname = join(path, tiff_parts[0] + '_rois.zip')

        # TODO TODO change. fname needs to always be under
        # analysis_output_root (or just change input in populate_db).
        # TODO or at least err if not subdir of it
        # see: https://stackoverflow.com/questions/3812849

        if _force_write_to is not None:
            if _force_write_to == True:
                fname = join(path, tiff_parts[0] + '_auto_rois.zip')
            else:
                fname = _force_write_to

        elif exists(fname):
            print(fname, 'already existed. returning.')
            return None, None

    if avg is None:
        if movie is None:
            movie = tifffile.imread(tif)

        avg = movie.mean(axis=0)

    # We enforce earlier that template must be symmetric.
    d, d2 = template.shape[::-1]
    assert d == d2

    keys = tiff_filename2keys(tif)
    ti_dir = thorimage_dir(*tuple(keys))
    xmlroot = get_thorimage_xmlroot(ti_dir)
    um_per_pixel_xy = get_thorimage_pixelsize_xml(xmlroot)

    # It seemed to me that picking a new threshold on cv2.TM_CCOEFF_NORMED was
    # not sufficient to reproduce cv2.TM_CCOEFF performance, so even if the
    # normed version were useful to keep the same threshold across image scales,
    # it seems other problems prevent me from using that in my case, so I'm
    # rescaling the image to match against.
    # TODO probably store target frame shape in template data store, rather than
    # hardcoding here
    target_frame_shape = (256, 256)
    frame_downscaling = 1.0
    if avg.shape != target_frame_shape:
        orig_frame_d = avg.shape[0]
        assert avg.shape[0] == avg.shape[1]

        avg = cv2.resize(avg, target_frame_shape)

        new_frame_d = avg.shape[0]
        frame_downscaling = orig_frame_d / new_frame_d
        um_per_pixel_xy *= frame_downscaling

    expected_cell_pixel_diam = mean_cell_extent_um / um_per_pixel_xy

    template_cell_pixel_diam = d - 2 * margin

    template_scale = expected_cell_pixel_diam / template_cell_pixel_diam
    new_template_d = int(round(template_scale * d))

    new_template_shape = tuple([new_template_d] * len(template.shape))
    if new_template_d != d:
        scaled_template = cv2.resize(template, new_template_shape)
        template_cell_pixel_diam *= new_template_d / d
    else:
        scaled_template = template

    # TODO maybe try histogram eq before template matching?
    # (or some other constrast enhancing transform, maybe one that
    # operates more locally?)

    res = template_match(avg, scaled_template, method_str=method_str)
    #imshow(res, 'match image')

    # TODO one fn that just returns circles, another to draw

    #eqd = cv2.equalizeHist(normed_u8(avg))
    #draw_on = eqd
    draw_on = avg

    #imshow(avg, 'avg')
    #imshow(eqd, 'equalized avg')

    # TODO TODO some multi-scale template matching? how to integrate w/
    # space constrained placement?

    radius = int(round(template_cell_pixel_diam / 2))

    # 0.5 seemed about OK as a threshold for normed ccoeff method
    # For non-normed ccoeff, 1000 low enough to pick up border stuff,
    # >3000 seems maybe too high, though 5000 still kinda reasonable.

    # Regarding exclusion_radius_frac: 0.3 allowed too much overlap, 0.5
    # borderline too much w/ non-normed method (0.7 OK there)
    # (r=4,er=4,6 respectively, in 0.5 and 0.7 cases)
    centers = greedy_roi_packing(res, radius, d, min_neighbors=min_neighbors,
        draw_on=draw_on, threshold=threshold,
        exclusion_radius_frac=exclusion_radius_frac,
        bboxes=False, nums=False, debug=debug
    )

    # TODO lower bound too, if any bounds?
    if len(centers) > max_cells_per_plane:
        raise RuntimeError('too many cells detected. try lowering threshold?')

    if frame_downscaling != 1.0:
        radius = int(round(radius * frame_downscaling))
        centers = np.round(centers * frame_downscaling).astype(centers.dtype)

    if write_ijrois:
        auto_md_fname = autoroi_metadata_filename(fname)

        name2bboxes = list()
        for i, center in enumerate(centers):
            # TODO TODO test that these radii are preserved across
            # round trip save / loads?
            min_corner = [center[0] - radius, center[1] - radius]
            max_corner = [
                min_corner[0] + 2 * radius,
                min_corner[1] + 2 * radius
            ]

            bbox = np.flip([min_corner, max_corner], axis=1)
            # TODO maybe this should be factored into ijroi?
            # does existing polygon writing code do this? or does that case
            # differ in the need for the offset for some reason?
            bbox = bbox + 1
            name2bboxes.append((str(i), bbox))

        print('Writing ImageJ ROIs to {} ...'.format(fname))
        ijroi.write_oval_roi_zip(name2bboxes, fname)

        with open(auto_md_fname, 'wb') as f:
            data = {
                'mtime': getmtime(fname)
            }
            pickle.dump(data, f)

    return centers, radius


def template_data_file():
    template_cache = 'template.p'
    return join(analysis_output_root(), template_cache)


def template_data(err_if_missing=False):
    template_cache = template_data_file()
    if exists(template_cache):
        with open(template_cache, 'rb') as f:
            data = pickle.load(f)
        return data
    else:
        if err:
            raise IOError(f'template data not found at {template_cache}')

        return None


def movie_blocks(tif, movie=None, allow_gsheet_to_restrict_blocks=True):
    """Returns list of arrays, one per continuous acquisition.

    Total length along time dimension should be preserved from input TIFF.
    """
    import tifffile
    from scipy import stats

    if movie is None:
        movie = tifffile.imread(tif)

    keys = tiff_filename2keys(tif)
    mat = matfile(*keys)
    ti = load_mat_timing_information(mat)

    # TODO TODO remove use_cache. just for testing.
    df = mb_team_gsheet(use_cache=True)
    #

    recordings = df.loc[(df.date == keys.date) &
                        (df.fly_num == keys.fly_num) &
                        (df.thorimage_dir == keys.thorimage_id)]
    recording = recordings.iloc[0]
    if recording.project != 'natural_odors':
        warnings.warn('project type {} not supported. skipping.'.format(
            recording.project))
        return

    # TODO factor this metadata handling out. fns for load / set?
    # combine w/ remy's .mat metadata (+ my stimfile?)

    meta = metadata(*keys)

    stimfile = recording['stimulus_data_file']
    stimfile_path = join(stimfile_root(), stimfile)
    # TODO also err if not readable / valid
    if not exists(stimfile_path):
        raise ValueError('copy missing stimfile {} to {}'.format(stimfile,
            stimfile_root))

    with open(stimfile_path, 'rb') as f:
        data = pickle.load(f)

    # TODO just infer from data if no stimfile and not specified in
    # metadata_file
    n_repeats = int(data['n_repeats'])

    # TODO delete this hack (which is currently just using new pickle
    # format as a proxy for the experiment being a supermixture experiment)
    if 'odor_lists' not in data:
        # The 3 is because 3 odors are compared in each repeat for the
        # natural_odors project.
        presentations_per_repeat = 3
        odor_list = data['odor_pair_list']
    else:
        n_expected_real_blocks = 3
        odor_list = data['odor_lists']
        # because of "block" def in arduino / get_stiminfo code
        # not matching def in randomizer / stimfile code
        # (scopePin pulses vs. randomization units, depending on settings)
        presentations_per_repeat = len(odor_list) // n_expected_real_blocks
        assert len(odor_list) % n_expected_real_blocks == 0

        # Hardcode to break up into more blocks, to align defs of blocks.
        # TODO (maybe just for experiments on 2019-07-25 ?) or change block
        # handling in here? make more flexible?
        n_repeats = 1

    presentations_per_block = n_repeats * presentations_per_repeat

    if pd.isnull(recording['first_block']):
        first_block = 0
    else:
        first_block = int(recording['first_block']) - 1

    if pd.isnull(recording['last_block']):
        n_full_panel_blocks = \
            int(len(odor_list) / presentations_per_block)
        last_block = n_full_panel_blocks - 1
    else:
        last_block = int(recording['last_block']) - 1

    first_presentation = first_block * presentations_per_block
    last_presentation = (last_block + 1) * presentations_per_block - 1

    odor_list = odor_list[first_presentation:(last_presentation + 1)]
    assert (len(odor_list) % (presentations_per_repeat * n_repeats) == 0)

    # TODO TODO delete odor frame stuff after using them to check blocks frames
    # are actually blocks and not trials
    # TODO or if keeping odor stuff, re-add asserts involving odor_list,
    # since how i have that here

    odor_onset_frames = np.array(ti['stim_on'], dtype=np.uint32
        ).flatten() - 1
    odor_offset_frames = np.array(ti['stim_off'], dtype=np.uint32).flatten() - 1
    assert len(odor_onset_frames) == len(odor_offset_frames)

    # Of length equal to number of blocks. Each element is the frame
    # index (from 1) in CNMF output that starts the block, where
    # block is defined as a period of continuous acquisition.
    block_first_frames = np.array(ti['block_start_frame'], dtype=np.uint32
        ).flatten() - 1
    block_last_frames = np.array(ti['block_end_frame'], dtype=np.uint32
        ).flatten() - 1

    n_blocks_from_gsheet = last_block - first_block + 1
    n_blocks_from_thorsync = len(block_first_frames)

    assert (len(odor_list) == (last_block - first_block + 1) *
        presentations_per_block)

    n_presentations = n_blocks_from_gsheet * presentations_per_block

    err_msg = ('{} blocks ({} to {}, inclusive) in Google sheet {{}} {} ' +
        'blocks from ThorSync.').format(n_blocks_from_gsheet,
        first_block + 1, last_block + 1, n_blocks_from_thorsync)
    fail_msg = (' Fix in Google sheet, turn off ' +
        'cache if necessary, and rerun.')

    if n_blocks_from_gsheet > n_blocks_from_thorsync:
        raise ValueError(err_msg.format('>') + fail_msg)

    elif n_blocks_from_gsheet < n_blocks_from_thorsync:
        if allow_gsheet_to_restrict_blocks:
            warnings.warn(err_msg.format('<') + (' This is ONLY ok if you '+
                'intend to exclude the LAST {} blocks in the Thor output.'
                ).format(n_blocks_from_thorsync - n_blocks_from_gsheet))
        else:
            raise ValueError(err_msg.format('<') + fail_msg)

    frame_times = np.array(ti['frame_times']).flatten()

    total_block_frames = 0
    for i, (b_start, b_end) in enumerate(
        zip(block_first_frames, block_last_frames)):

        if i != 0:
            last_b_end = block_last_frames[i - 1]
            assert last_b_end == (b_start - 1)

        assert (b_start < len(frame_times)) and (b_end < len(frame_times))
        block_frametimes = frame_times[b_start:b_end]
        dts = np.diff(block_frametimes)
        # np.max(np.abs(dts - np.mean(dts))) / np.mean(dts)
        # was 0.000148... in one case I tested w/ data from the older
        # system, so the check below w/ rtol=1e-4 would fail.
        mode = stats.mode(dts)[0]
        assert np.allclose(dts, mode, rtol=3e-4)

        total_block_frames += b_end - b_start + 1

    orig_n_frames = movie.shape[0]
    # TODO may need to remove this assert to handle cases where there is a
    # partial block (stopped early). leave assert after slicing tho.
    # (warn instead, probably)
    assert total_block_frames == orig_n_frames, \
        '{} != {}'.format(total_block_frames, orig_n_frames)

    if allow_gsheet_to_restrict_blocks:
        # TODO unit test for case where first_block != 0 and == 0
        # w/ last_block == first_block and > first_block
        # TODO TODO doesn't this only support dropping blocks at end?
        # do i assert that first_block is 0 then? probably should...
        # TODO TODO TODO shouldnt it be first_block:last_block+1?
        block_first_frames = block_first_frames[
            :(last_block - first_block + 1)]
        block_last_frames = block_last_frames[
            :(last_block - first_block + 1)]

        assert len(block_first_frames) == n_blocks_from_gsheet
        assert len(block_last_frames) == n_blocks_from_gsheet

        # TODO also delete this odor frame stuff when done
        odor_onset_frames = odor_onset_frames[
            :(last_presentation - first_presentation + 1)]
        odor_offset_frames = odor_offset_frames[
            :(last_presentation - first_presentation + 1)]

        assert len(odor_onset_frames) == n_presentations
        assert len(odor_offset_frames) == n_presentations
        #

        frame_times = frame_times[:(block_last_frames[-1] + 1)]

    last_frame = block_last_frames[-1]

    n_tossed_frames = movie.shape[0] - (last_frame + 1)
    if n_tossed_frames != 0:
        print(('Tossing trailing {} of {} frames of movie, which did not ' +
            'belong to any used block.\n').format(
            n_tossed_frames, movie.shape[0]))

    # TODO want / need to do more than just slice to free up memory from
    # other pixels? is that operation worth it?
    drop_first_n_frames = meta['drop_first_n_frames']
    # TODO TODO err if this is past first odor onset (or probably even too
    # close)

    odor_onset_frames = [n - drop_first_n_frames
        for n in odor_onset_frames]
    odor_offset_frames = [n - drop_first_n_frames
        for n in odor_offset_frames]

    block_first_frames = [n - drop_first_n_frames
        for n in block_first_frames]
    block_first_frames[0] = 0
    block_last_frames = [n - drop_first_n_frames
        for n in block_last_frames]

    assert odor_onset_frames[0] > 0

    frame_times = frame_times[drop_first_n_frames:]
    movie = movie[drop_first_n_frames:(last_frame + 1)]

    # TODO TODO fix bug referenced in cthulhu:190520...
    # and re-enable assert
    assert movie.shape[0] == len(frame_times), \
        '{} != {}'.format(movie.shape[0], len(frame_times))
    #

    if movie.shape[0] != len(frame_times):
        warnings.warn('{} != {}'.format(movie.shape[0], len(frame_times)))

    # TODO maybe move this and the above checks on block start/end frames
    # + frametimes into assign_frames_to_trials
    n_frames = movie.shape[0]
    total_block_frames = sum([e - s + 1 for s, e in
        zip(block_first_frames, block_last_frames)])

    assert total_block_frames == n_frames, \
        '{} != {}'.format(total_block_frames, n_frames)


    # TODO any time / space diff returning slices to slice array and only
    # slicing inside loop vs. returning list of (presumably views) by slicing
    # matrix?
    blocks = [movie[start:(stop + 1)] for start, stop in
        zip(block_first_frames, block_last_frames)]
    assert sum([b.shape[0] for b in blocks]) == movie.shape[0]
    return blocks


def downsample_movie(movie, target_fps, current_fps, allow_overshoot=True,
    allow_uneven_division=False, relative_fps_err=True, debug=False):
    """Returns downsampled movie by averaging consecutive groups of frames.

    Groups of frames averaged do not overlap.
    """
    if allow_uneven_division:
        raise NotImplementedError

    # TODO maybe kwarg for max acceptable (rel/abs?) factor error,
    # and err / return None if it can't be achieved

    target_factor = current_fps / target_fps
    if debug:
        print(f'allow_overshoot: {allow_overshoot}')
        print(f'allow_uneven_division: {allow_uneven_division}')
        print(f'relative_fps_err: {relative_fps_err}')
        print(f'target_fps: {target_fps:.2f}\n')
        print(f'target_factor: {target_factor:.2f}\n')

    n_frames = movie.shape[0]

    # TODO TODO also support uneven # of frames per bin (toss last probably)
    # (skip loop checking for even divisors in that case)

    # Find the largest/closest downsampling we can do, with equal numbers of
    # frames for each average.
    best_divisor = None
    for i in range(1, n_frames):
        if n_frames % i != 0:
            continue

        decimated_n_frames = n_frames // i
        # (will always be float(i) in even division case, so could get rid of
        # this if that's all i'll support)
        factor = n_frames / decimated_n_frames
        if debug:
            print(f'factor: {factor:.2f}')

        if factor > target_factor and not allow_overshoot:
            if debug:
                print('breaking because of overshoot')
            break

        downsampled_fps = current_fps / factor
        fps_error = downsampled_fps - target_fps
        if relative_fps_err:
            fps_error = fps_error / target_fps

        if debug:
            print(f'downsampled_fps: {downsampled_fps:.2f}')
            print(f'fps_error: {fps_error:.2f}')

        if best_divisor is None or abs(fps_error) < abs(best_fps_error):
            best_divisor = i
            best_downsampled_fps = downsampled_fps
            best_fps_error = fps_error
            best_factor = factor

            if debug:
                print(f'best_downsampled_fps: {best_downsampled_fps:.2f}')
                print('new best factor')

        elif (best_divisor is not None and
            abs(fps_error) > abs(best_fps_error)):

            assert allow_overshoot
            if debug:
                print('breaking because past best factor')
            break

        if debug:
            print('')

    assert best_divisor is not None

    # TODO unit test for this case
    if best_divisor == 1:
        raise ValueError('best downsampling with this flags at factor of 1')

    if debug:
        print(f'best_divisor: {best_divisor}')
        print(f'best_factor: {best_factor:.2f}')
        print(f'best_fps_error: {best_fps_error:.2f}')
        print(f'best_downsampled_fps: {best_downsampled_fps:.2f}')

    frame_shape = movie.shape[1:]
    new_n_frames = n_frames // best_divisor

    # see: stackoverflow.com/questions/15956309 for how to adapt this
    # to uneven division case
    downsampled = movie.reshape((new_n_frames, best_divisor) + frame_shape
        ).mean(axis=1)

    # TODO maybe it's obvious, but is there any kind of guarantee dimensions in
    # frame_shape will not be screwed up in a way relevant to the average
    # when reshaping?
    # well, at least this looks reasonable:
    # image_grid(downsampled[:64])

    return downsampled, best_downsampled_fps


# TODO maybe move to ijroi
# don't like this convexHull based approach though...
# (because roi may be intentionally not a convex hull)
def ijroi2cv_contour(roi):
    import cv2

    ## cnts = cv2.findContours(img, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    ## cnts[1][0].shape
    ## cnts[1][0].dtype
    # from inspecting output of findContours, as above:
    #cnt = np.expand_dims(ijroi, 1).astype(np.int32)
    # TODO fix so this isn't necessary. in case of rois that didn't start as
    # circles, the convexHull may occasionally not be equal to what i want
    cnt = cv2.convexHull(roi.astype(np.int32))
    # if only getting cnt from convexHull, this is probably a given...
    assert cv2.contourArea(cnt) > 0
    return cnt
#


def roi_center(roi):
    import cv2
    cnt = ijroi2cv_contour(roi)
    M = cv2.moments(cnt)
    cx = int(M['m10'] / M['m00'])
    cy = int(M['m01'] / M['m00'])
    return np.array((cx, cy))


def roi_centers(rois):
    centers = []
    for roi in rois:
        center = roi_center(roi)
        # pretty close to (w/in 0.5 in each dim) np.mean(roi, axis=0),
        # in at least one example i played with
        centers.append(center)
    return np.array(centers)


def tiff_title(tif):
    """Returns abbreviation of TIFF filename for use in titles.
    """
    parts = [x for x in tif.split('/')[-4:] if x != 'tif_stacks']
    ext = '.tif'
    if parts[-1].endswith(ext):
        parts[-1] = parts[-1][:-len(ext)]
    return '/'.join(parts)


# TODO didn't i have some other fn for this? delete one if so
# (or was it just in natural_odors?)
def to_filename(title):
    return title.replace('/','_').replace(' ','_').replace(',','').replace(
        '.','') + '.'


def correspond_rois(left_centers_or_seq, *right_centers, cost_fn=euclidean_dist,
    max_cost=9, show=True, left_name='Left', right_name='Right', name_prefix='',
    draw_on=None, title='', colors=None, connect_centers=True,
    pairwise_plots=True, pairwise_same_style=False, roi_numbers=False,
    jitter=True, progress=False):
    """
    Args:
    left_centers_or_seq (list): (length n_timepoints) list of (n_rois x 2)
        arrays of ROI center coordinates.

    Returns:
    lr_matches: list of arrays matching ROIs in one timepoint to ROIs in the
        next.

    left_unmatched: list of arrays with ROI labels at time t,
        without a match at time (t + 1)

    right_unmatched: same as left_unmatched, but for (t + 1) with respect to t.

    total_costs: array of sums of costs from matching.
    
    fig: matplotlib figure handle to the figure with all ROIs on it,
        for modification downstream.
    """
    # TODO doc support for ROI inputs / rewrite to expect them
    # (to use jaccard, etc)

    from scipy.optimize import linear_sum_assignment
    import seaborn as sns
    if progress:
        from tqdm import tqdm

    # TODO maybe unsupport two args case to be more concise
    if len(right_centers) == 0:
        sequence_of_centers = left_centers_or_seq

    elif len(right_centers) == 1:
        right_centers = right_centers[0]
        sequence_of_centers = [left_centers_or_seq, right_centers]

    else:
        raise ValueError('wrong number of arguments')

    if max_cost is None:
        raise ValueError('max_cost must not be None')

    default_two_colors = ['red', 'blue']
    if len(sequence_of_centers) == 2:
        pairwise_plots = False
        scatter_alpha = 0.6
        scatter_marker = None
        labels = [n + ' centers' for n in (left_name, right_name)]
        if colors is None:
            colors = default_two_colors
    else:
        scatter_alpha = 0.8
        scatter_marker = 'x'
        labels = [name_prefix + str(i) for i in range(len(sequence_of_centers))]
        if colors is None:
            colors = sns.color_palette('hls', len(sequence_of_centers))

    for i, centers in enumerate(sequence_of_centers):
        # Otherwise it should be an ndarray representing centers
        # TODO assertion on dims in ndarray case
        if type(centers) is list:
            sequence_of_centers[i] = roi_centers(centers)

    fig = None
    if show:
        figsize = (10, 10)
        fig, ax = plt.subplots(figsize=figsize)
        if draw_on is None:
            color = 'black'
        else:
            ax.imshow(draw_on, cmap='gray')
            ax.axis('off')
            color = 'yellow'
        fontsize = 8
        text_x_offset = 2
        plot_format = 'png'

        if jitter:
            np.random.seed(50)
            jl = -0.1
            jh = 0.1

    unmatched_left = []
    unmatched_right = []
    lr_matches = []
    cost_totals = []

    if progress:
        centers_iter = tqdm(range(len(sequence_of_centers) - 1))
        print('Matching ROIs across timepoints:')
    else:
        centers_iter = range(len(sequence_of_centers) - 1)

    for k in centers_iter:
        left_centers = sequence_of_centers[k]
        right_centers = sequence_of_centers[k + 1]

        # TODO other / better ways to generate cost matrix?
        # pairwise jacard (would have to not take centers then)?
        # TODO why was there a "RuntimeWarning: invalid valid encounterd in
        # multiply" here ocassionally? it still seems like we had some left and
        # right centers, so idk
        costs = np.empty((len(left_centers), len(right_centers))) * np.nan
        for i, cl in enumerate(left_centers):
            for j, cr in enumerate(right_centers):
                # TODO short circuit as appropriate? better way to loop over
                # coords we need?
                cost = cost_fn(cl, cr)
                if cost > max_cost:
                    cost = max_cost
                costs[i,j] = cost

        # TODO was Kellan's method of matching points not equivalent to this?
        # or per-timestep maybe it was (or this was better), but he also
        # had a way to evolve points over time (+ a particular cost)?

        left_idx, right_idx = linear_sum_assignment(costs)
        # Just to double-check properties I assume about the assignment
        # procedure.
        assert len(left_idx) == len(np.unique(left_idx))
        assert len(right_idx) == len(np.unique(right_idx))

        n_not_drawn = None
        if show:
            if jitter:
                left_jitter = np.random.uniform(low=jl, high=jh,
                    size=left_centers.shape)
                right_jitter = np.random.uniform(low=jl, high=jh,
                    size=right_centers.shape)

                left_centers_to_plot = left_centers + left_jitter
                right_centers_to_plot = right_centers + right_jitter
            else:
                left_centers_to_plot = left_centers
                right_centers_to_plot = right_centers

            if pairwise_plots:
                # TODO maybe change multiple pairwise plots to be created as
                # axes within one the axes from one call to subplots
                pfig, pax = plt.subplots(figsize=figsize)
                if pairwise_same_style:
                    pmarker = scatter_marker
                    c1 = colors[k]
                    c2 = colors[k + 1]
                else:
                    pmarker = None
                    c1 = default_two_colors[0]
                    c2 = default_two_colors[1]

                if draw_on is not None:
                    pax.imshow(draw_on, cmap='gray')
                    pax.axis('off')

                pax.scatter(*left_centers_to_plot.T, label=labels[k],
                    color=c1, alpha=scatter_alpha,
                    marker=pmarker)

                pax.scatter(*right_centers_to_plot.T, label=labels[k + 1],
                    color=c2, alpha=scatter_alpha,
                    marker=pmarker)

                psuffix = f'{k} vs. {k+1}'
                if len(name_prefix) > 0:
                    psuffix = f'{name_prefix} ' + psuffix
                if len(title) > 0:
                    ptitle = f'{title}, ' + psuffix
                else:
                    ptitle = psuffix
                pax.set_title(ptitle)
                pax.legend()

            ax.scatter(*left_centers_to_plot.T, label=labels[k],
                color=colors[k], alpha=scatter_alpha,
                marker=scatter_marker)

            # TODO factor out scatter + opt numbers (internal fn?)
            if roi_numbers:
                for i, (x, y) in enumerate(left_centers_to_plot):
                    ax.text(x + text_x_offset, y, str(i),
                        color=colors[k], fontsize=fontsize)

            # Because generally this loop only scatterplots the left_centers,
            # so without this, the last set of centers would not get a
            # scatterplot.
            if (k + 1) == (len(sequence_of_centers) - 1):
                last_centers = right_centers_to_plot

                ax.scatter(*last_centers.T, label=labels[-1],
                    color=colors[-1], alpha=scatter_alpha,
                    marker=scatter_marker)

                if roi_numbers:
                    for i, (x, y) in enumerate(last_centers):
                        ax.text(x + text_x_offset, y, str(i),
                            color=colors[-1], fontsize=fontsize)

            if connect_centers:
                n_not_drawn = 0
                for li, ri in zip(left_idx, right_idx):
                    if costs[li,ri] >= max_cost:
                        n_not_drawn += 1
                        continue
                        #linestyle = '--'
                    else:
                        linestyle = '-'

                    lc = left_centers_to_plot[li]
                    rc = right_centers_to_plot[ri]
                    correspondence_line = ([lc[0], rc[0]], [lc[1], rc[1]])

                    ax.plot(*correspondence_line, linestyle=linestyle,
                        color=color, alpha=0.7)

                    if pairwise_plots:
                        pax.plot(*correspondence_line, linestyle=linestyle,
                            color=color, alpha=0.7)

                # TODO didn't i have some fn for getting filenames from things
                # like titles? use that if so
                # TODO plot format + flag to control saving + save to some
                # better dir
                # TODO separate dir for these figs? or at least place where some
                # of other figs currently go?
                if pairwise_plots:
                    fname = to_filename(ptitle) + plot_format
                    print(f'writing to {fname}')
                    pfig.savefig(fname)

        k_unmatched_left = set(range(len(left_centers))) - set(left_idx)
        k_unmatched_right = set(range(len(right_centers))) - set(right_idx)

        # TODO why is costs.min() actually 0? that seems unlikely?
        match_costs = costs[left_idx, right_idx]
        total_cost = match_costs.sum()

        to_unmatch = match_costs >= max_cost
        # For checking consistent w/ draw output above
        if n_not_drawn is not None:
            n_unmatched = to_unmatch.sum()
            assert n_not_drawn == n_unmatched, f'{n_not_drawn} != {n_unmatched}'

        k_unmatched_left.update(left_idx[to_unmatch])
        k_unmatched_right.update(right_idx[to_unmatch])
        left_idx = left_idx[~ to_unmatch]
        right_idx = right_idx[~ to_unmatch]

        n_unassigned = abs(len(left_centers) - len(right_centers))

        total_cost += max_cost * n_unassigned
        # TODO better way to normalize error?
        total_cost = total_cost / max(len(left_centers), len(right_centers))

        unmatched_left.append(np.array(list(k_unmatched_left)))
        unmatched_right.append(np.array(list(k_unmatched_right)))
        lr_matches.append(np.stack([left_idx, right_idx], axis=-1))
        cost_totals.append(total_cost)

    if show:
        ax.legend()
        ax.set_title(title)

        # TODO and delete this extra hack
        if len(sequence_of_centers) > 2:
            extra = '_acrossblocks'
        else:
            extra = ''
        fname = to_filename(title + extra) + plot_format
        #
        print(f'writing to {fname}')
        fig.savefig(fname)
        #

    if len(sequence_of_centers) == 2:
        lr_matches = lr_matches[0]
        unmatched_left = unmatched_left[0]
        unmatched_right = unmatched_right[0]
        cost_totals = cost_totals[0]

    # TODO maybe stop returning unmatched_* . not sure it's useful.

    return lr_matches, unmatched_left, unmatched_right, cost_totals, fig


def stable_rois(lr_matches, verbose=False):
    """
    Takes a list of n_cells x 2 matrices, with each row taking an integer ROI
    label from one set of labels to the other.

    Input is as first output of correspond_rois.

    Returns:
    stable_cells: a n_stable_cells x (len(lr_matches) + 1) matrix, where rows
        represent different labels for the same real cells. Columns have the
        set of stable cells IDs, labelled as the inputs are.

    new_lost: a (len(lr_matches) - 1) length list of IDs lost when matching
        lr_matches[i] to lr_matches[i + 1]. only considers IDs that had
        been stable across all previous pairs of matchings.
    """
    # TODO TODO also test in cases where lr_matches is greater than len 2
    # (at least len 3)

    # TODO TODO also test when lr_matches is len 1, to support that case
    if len(lr_matches) == 1 or type(lr_matches) is not list:
        raise NotImplementedError

    orig_matches = lr_matches
    # Just since it gets written to in the loop.
    lr_matches = [m.copy() for m in lr_matches]

    stable = lr_matches[0][:,0]
    UNLABELLED = -1
    new_lost = []
    for i in range(len(lr_matches) - 1):
        matches1 = lr_matches[i]
        matches2 = lr_matches[i + 1]

        # These two columns should have the ROI / center numbers
        # represent the same real ROI / point coordinates.
        stable_1to2, m1_idx, m2_idx = np.intersect1d(
            matches1[:,1], matches2[:,0], return_indices=True)

        assert np.array_equal(matches1[m1_idx, 1], matches2[m2_idx, 0])

        curr_stable_prior_labels = matches1[m1_idx, 0]

        matches2[m2_idx, 0] = curr_stable_prior_labels

        # To avoid confusion / errors related too using old, now meaningless
        # labels.
        not_in_m2_idx = np.setdiff1d(np.arange(len(matches2)), m2_idx)
        assert (lr_matches[i + 1] == UNLABELLED).sum() == 0
        matches2[not_in_m2_idx] = UNLABELLED 
        assert (lr_matches[i + 1] == UNLABELLED).sum() == 2 * len(not_in_m2_idx)

        ids_lost_at_i = np.setdiff1d(stable, curr_stable_prior_labels)
        stable = np.setdiff1d(stable, ids_lost_at_i)
        new_lost.append(ids_lost_at_i)

        n_lost_at_i = len(ids_lost_at_i)
        if verbose and n_lost_at_i > 0:
            print(f'Lost {n_lost_at_i} ROI(s) between blocks {i} and {i + 1}')

    # TODO make a test case where the total number of *matched* rois is
    # conserved at each time step, but the matching makes the length of the
    # ultimate stable set reduce
    n_matched = [len(m) - ((m == UNLABELLED).sum() / 2) for m in lr_matches]
    assert len(stable) <= min(n_matched)

    stable_cells = []
    for i, matches in enumerate(lr_matches):
        # Because each of these columns will have been edited in the loop
        # above, to have labels matching the first set of center labels.
        _, _, stable_indices_i = np.intersect1d(stable, matches[:,0],
            return_indices=True)

        assert not UNLABELLED in matches[stable_indices_i, 0]
        orig_labels_stable_i = orig_matches[i][stable_indices_i, 0]
        stable_cells.append(orig_labels_stable_i)

    # This last column in the last element in the last of matches
    # was the only column that did NOT get painted over with the new labels.
    stable_cells.append(matches[stable_indices_i, 1])
    stable_cells = np.stack(stable_cells, axis=1)

    # might be redundant...
    stable_cells = stable_cells[np.argsort(stable_cells[:,0]), :]
    assert np.array_equal(stable_cells[:,0], stable)
    return stable_cells, new_lost


# TODO TODO should either this fn or correspond_rois try to handle the case
# where a cell drifts out of plane and then back into plane???
# possible? some kind of filtering?
def renumber_rois(matches_list, centers_list):
    """
    Each sequence of matched ROIs gets an increasing integer identifier
    (including length-1 sequences, i.e. unmatched stuff).

    Returns lists of IDs in each element of input list and centers,
    re-indexed with new IDs.
    """
    # TODO TODO pad w/ NaN / UNLABELLED so that each element in 
    # output list can be made of equal length (# of unique IDs across all)
    # and then fit it all into an array
    # TODO TODO do the same with centers

    # TODO use this function inside stable_rois / delete that function
    # altogether (?)

    if type(matches_list) is not list or type(centers_list) is not list:
        raise ValueError('both input arguments must be lists')

    if len(matches_list) == 1:
        raise NotImplementedError

    assert len(centers_list) == len(matches_list) + 1

    # Since they get written to in the loop.
    matches_list = [m.copy() for m in matches_list]
    centers_list = [c.copy() for c in centers_list]

    ids_list = []
    first_ids = matches_list[0][:, 0]
    next_new_id = first_ids.max() + 1
    # This also checks it's sorted, b/c unique sorts.
    # Because these are sorted, don't need to re-order centers_list[0].
    assert np.array_equal(np.unique(first_ids), first_ids)
    ids_list.append(first_ids)

    # TODO correct?
    '''
    # don't think so... it should be something usable to index centers
    # (though prob also indep need something to fill in ids... ?)
    '''

    # To handle case where loop isn't entered
    # (len(matches_list) == 1)
    last_column_idx = np.arange(len(matches_list[0]))

    for i in range(len(matches_list) - 1):
        matches1 = matches_list[i]
        matches2 = matches_list[i + 1]

        # TODO TODO maybe assert that first column of matches1 is always sorted?
        # (should it be? i mean we have re-ordered centers, and wasn't that kind
        # of the point? or should propagated ids not have that value for some
        # reason...?)

        # These two columns should have the ROI / center numbers
        # represent the same real ROI / point coordinates.
        shared_center_ids, shared_m1_idx, shared_m2_idx = np.intersect1d(
            matches1[:,1], matches2[:,0], return_indices=True)

        assert np.array_equal(
            matches1[shared_m1_idx, 1],
            matches2[shared_m2_idx, 0]
        )

        # These centers are referred to by the IDs in matches_list[i + 1][:, 1],
        # and (if it exists) matches_list[i + 2][:, 1]
        centers = centers_list[i + 1]
        assert len(matches2) <= len(centers)

        # These include both things in matches2 (those not shared with matches1)
        # and things we need to generate new IDs for.
        #other_m2_center_ids = np.setdiff1d(np.arange(len(centers)),
        #    shared_m2_idx)
        other_m2_center_ids = np.setdiff1d(np.arange(len(centers)),
            shared_center_ids)

        # This should be of the same length as centers and should index each
        # value, just in a different order.
        #new_center_idx = np.concatenate((shared_m2_idx, other_m2_center_ids))
        new_center_idx = np.concatenate((shared_center_ids,
            other_m2_center_ids))
        assert np.array_equal(np.arange(len(centers)),
            np.unique(new_center_idx))

        # We are re-ordering the centers, so that they are in the same order
        # as the IDs (both propagated and new) at this timestep (curr_ids).
        reordered_centers = centers[new_center_idx]
        # (loop starts at i=0 and we do not need to re-order first array of
        # centers. centers_list is also 1 longer than matches_list, so (i + 1)
        # will never index the last element of centers_list.)
        centers_list[i + 1] = reordered_centers

        n_new_ids = len(other_m2_center_ids)
        # Not + 1 because arange does not include the endpoint.
        stop = next_new_id + n_new_ids
        new_ids = np.arange(next_new_id, stop)
        next_new_id = stop

        prior_ids_of_shared = matches1[shared_m1_idx, 0]
        matches2[shared_m2_idx, 0] = prior_ids_of_shared

        # TODO TODO may need to fix
        '''
        nonshared_m2_idx = other_m2_center_ids[
            other_m2_center_ids < len(matches2)]
        # ROIs unmatched in matches2 get any remaining higher IDs in new_ids
        matches2[nonshared_m2_idx, 0] = new_ids[:len(nonshared_m2_idx)]
        '''

        assert len(np.intersect1d(shared_m2_idx, nonshared_m2_idx)) == 0

        curr_ids = np.concatenate((prior_ids_of_shared, new_ids))
        assert len(curr_ids) == len(centers_list[i + 1])
        assert len(curr_ids) == len(np.unique(curr_ids))

        ids_list.append(curr_ids)

        last_column_idx = np.concatenate(shared_m2_idx, nonshared_m2_idx)
        # TODO some assert on what last col indexed by last_column_idx is?

    last_matches = matches_list[-1]
    last_centers = centers_list[-1]
    assert len(last_matches) <= len(last_centers)
    n_new_ids = len(last_centers) - len(last_matches)

    stop = next_new_id + n_new_ids
    new_ids = np.arange(next_new_id, stop)
    # Not + 1 because arange does not include the endpoint.
    next_new_id = stop

    '''
    other_m2_center_ids = np.setdiff1d(np.arange(len(last_centers)),
        )
    '''

    # TODO uncomment
    '''
    # TODO TODO reorder last centers
    last_center_idx = 
    centers_list[-1] = last_centers[last_center_idx]

    # TODO TODO 
    ids_list.append(
    '''

    # TODO TODO need to special case end? (yes, just a matter of how)
    # TODO how to reorder centers there?

    # TODO TODO some more reasonable representation besides mask?
    # (id, start, stop) tuples (if no re-assignment to ID after losing it...)?
    # TODO should this be boolean (true for presence?)
    #ids_array = np.empty((next_new_id, len(centers_list))) * np.nan
    ids_array = np.zeros((next_new_id, len(centers_list)), dtype=bool)

    centers_array = np.empty((next_new_id, len(centers_list), 2)) * np.nan

    for i, (ids, centers) in enumerate(zip(ids_list, centers_list)):
        ids_array[ids, i] = True
        centers_array[ids, i, :] = centers

    # TODO pad stuff / fill the above in

    import ipdb; ipdb.set_trace()

    return ids_array, centers_array


# Adapted from Vishal's answer at https://stackoverflow.com/questions/287871
_color_codes = {
    'red': '31',
    'green': '32',
    'yellow': '33',
    'blue': '34',
    'cyan': '36'
}
def start_color(color_name):
    try:
        color_code = _color_codes[color_name]
    except KeyError as err:
        print('Available colors are:')
        pprint.pprint(list(_color_codes.keys()))
        raise
    print('\033[{}m'.format(color_code), end='')


def stop_color():
    print('\033[0m', end='')


def print_color(color_name, *args, **kwargs):
    start_color(color_name)
    print(*args, **kwargs, end='')
    stop_color()

