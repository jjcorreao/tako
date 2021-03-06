#!/usr/bin/env python
"""
Find and report status of Tigres activities as exposed by the
monitoring information (e.g. logs).

Note: This may move to some other file later..
"""
from tigres.core.state.work import WorkSequence, WorkParallel, WorkUnit
from tigres.utils import State

__author__ = 'Dan Gunter <dkgunter@lbl.gov>'
__date__ = '3/15/13'

import logging
import os
import sys
import argparse
import re

from tigres.core.graph import dot_execution_string

try:
    from urllib import parse
except ImportError:
    import urlparse

    parse = urlparse
#
from tigres.core.monitoring import log, Keyword
from tigres.core.monitoring import kvp

g_log = logging.getLogger('log.py')


def report_str(s):
    print(s)


def report_status(o):
    """Print status of a given node.
    """
    if o.state == log.State.DONE:
        if o.errcode == 0:
            print("{t},{type},{n},Success".format(type=o.node_type, t=o.timestr, n=o.name))
        else:
            print("{t},{type},{n},Failure {c:d} {m}".format(type=o.node_type, t=o.timestr, n=o.name, m=o.errmsg, c=o.errcode))
    else:
        print("{t},{type},{n},{s}".format(type=o.node_type, t=o.timestr, n=o.name, s=o.state.title()))


def check_main(args):
    """Main program for 'status' mode.

    :return: Status of run, 0=OK
    :rtype: int
    """
    return_code = 0
    if args.task_t:
        nodetype = log.NodeType.TASK
    elif args.tmpl_t:
        nodetype = log.NodeType.TEMPLATE
    elif args.program_t:
        nodetype = log.NodeType.PROGRAM
    else:
        nodetype = log.NodeType.ANY
    namelist = args.names
    kvp.Reader.ignore_bad = args.bad_ok
    statuses = None
    program_id = get_last_program_id()
    try:
        statuses = log.check(nodetype, names=namelist, multiple=args.multiple, program_id=program_id)
    except ValueError as err:
        g_log.error(str(err))
        return_code = -1
    if return_code == 0:
        if not statuses:
            report_str("Nothing found")
        else:
            for obj in statuses:
                report_status(obj)
    return return_code


def user_main(args):
    """Main program for 'user' mode

    :return: Status of run, 0=OK
    :rtype: int
    """
    g_log.info("query.begin")
    if args.fmt == 'json':
        w = kvp.LogWriter(sys.stdout, fmt=log.LOG_FORMAT_JSON)
    elif args.fmt == 'kvp':
        w = kvp.LogWriter(sys.stdout, fmt=log.LOG_FORMAT_NL)
    elif args.fmt == 'table':
        w = kvp.TableWriter(sys.stdout, idlen=args.idlen, pagelen=args.pagelen)
    return_code = -1
    if args.fields:
        fields = [x.strip() for x in args.fields.split(',')]
    else:
        fields = None
    try:
        for rec in log.query(spec=args.exprs):
            if fields:
                rec.project(fields, copy=False)
            w.put(rec)
        w.close()
        return_code = 0
    except log.BuildQueryError as err:
        g_log.error("Invalid query: {}".format(err))
    except ValueError as err:
        g_log.error(str(err))
    g_log.info("query.end")
    return return_code


def graph_from_log(program_id):
    program = None

    # Get the templates for the given program
    status_templates = log.check('template', program_id=program_id)
    for status_template in status_templates:

        # Create the root_work sequence representation of the program (workflow)
        if not program:
            program = WorkSequence(None, status_template.program_name)

        # Get all of the tasks for the current template
        status_tasks = log.check("task", template_id=status_template.name, program_id=program_id)
        if not status_tasks:
            break

        if status_template.node_type.endswith("parallel"):
            # Parallel Template
            template = WorkParallel(program, status_template.name)
            program.append(template)

            for task in status_tasks:
                template.append(WorkUnit(template, task.name, task.state))

        else:
            template = WorkSequence(program, status_template.name)
            program.append(template)
            if status_template.node_type.endswith("merge"):
                # Merge Template

                # Need to check if there was a parallel task
                status_parallel = log.check("parallel", template_id=status_template.name, program_id=program_id)

                if status_parallel:
                    # Assuming there is only one status (Revisit with nested templates)
                    parallel = status_parallel[0]
                    work_parallel = WorkParallel(template, template.name)
                    template.append(work_parallel)

                    for task in status_tasks[:-1]:
                        work_parallel.append(WorkUnit(work_parallel, task.name, task.state))

                    # Determine if the parallel tasks finished successfully
                    # This fact decides where the last task goes.
                    last_task = WorkUnit(None, status_tasks[-1].name, status_tasks[-1].state)
                    if parallel.state != State.DONE:
                        work_parallel.append(last_task)
                    else:
                        template.append(last_task)
            elif status_template.node_type.endswith("split"):
                # Split Template
                template.append(WorkUnit(None, status_tasks[0].name, status_tasks[0].state))
                work_parallel = WorkParallel(template, template.name)
                template.append(work_parallel)

                if len(status_tasks) > 1:
                    for task in status_tasks[1:]:
                        work_parallel.append(WorkUnit(work_parallel, task.name, task.state))
            elif status_template.node_type.endswith("sequence"):
                for task in status_tasks:
                    template.append(WorkUnit(template, task.name, task.state))

    return dot_execution_string(program)


def get_last_program_id():
    for rec in log.query(['{} != user'.format(Keyword.EVENT), '{} = {}'.format(Keyword.EVENT, "RUN"),
                              '{} ~ {}'.format(Keyword.NODETYPE, "template.*")]):
        prgm_id = rec[Keyword.PROGRAM_UID]
    assert prgm_id != None
    return prgm_id


def graph_main(args):
    """
    Constructs dot graph
    @input: args from main()
    @returns: dot graph as a string
    """

    prgm_id = args.prgm_id
    if not prgm_id:
        prgm_id = get_last_program_id()

    graph_dot_string = graph_from_log(prgm_id)

    """ write to specified file & path, if specified """
    if args.out_path:
        file_name = args.out_path.split('/')[-1]
        if file_name.split('.')[-1] != "dot":
            default_file_path = args.out_path.rstrip("/") + "/" + re.search('(.*[/]+)(.*)(\..*)', args.url).group(2) + "_graph.dot"
            with open(default_file_path, 'w') as out_file:
                out_file.write(graph_dot_string)
        else:
            with open(args.out_path, 'w') as out_file:
                out_file.write(graph_dot_string)
    else:
        return graph_dot_string


def main(cmdline=sys.argv[1:]):
    """Program entry point.

    :return: Status of run, 0=OK
    :rtype: int
    """
    return_code = 0

    parser = argparse.ArgumentParser()
    parser.add_argument('-b', '--badok', dest='bad_ok', action='store_true',
                        help='Ignore un-parseable records')
    parser.add_argument('-v', '--verbose', dest='vb', action='count', default=0,
                        help='Increase log message verbosity (to stderr)')
    url_help = "Log path or URL, e.g., /var/log/mylogfile'."
    subp = parser.add_subparsers(help='Valid query modes', title='Query mode')
    # 'status' mode
    check_parser = subp.add_parser('check', help='Check the status of task, template, or workflow')
    check_parser.add_argument('-a', '--all', dest='multiple', action='store_true',
                              help='Return multiple (all) results [default=last one only]')
    check_parser.add_argument('-n', '--name', dest='names', action='append', metavar='EXPR',
                              help='Name of component to look for, as text or regular expression.')
    check_parser.add_argument('--task', dest='task_t', action='store_true', help='Component is a Tigres task')
    check_parser.add_argument('--template', dest='tmpl_t', action='store_true', help='Component is a Tigres template')
    check_parser.add_argument('--program', dest='program_t', action='store_true', help='Component is a Tigres program')
    check_parser.add_argument('url', help=url_help)
    check_parser.set_defaults(main_fn=check_main)
    # 'query' mode
    user_parser = subp.add_parser('query', help='Query the logs', description="""
query expressions are in the form: <field> <operation> <value>.
  <field>     is the name of the field in the log record, e.g., 'level' or any user-defined field.
  <operation> is a boolean operation. Defined operations are: >, >=, <, <=, =, ~. The last one is
              the regular expression operator.
  <value>    is the value to match against. The first four (inequalities) require numeric values,
              '=' does an exact string match, and '~' makes its value into a regular expression.""",
                                  epilog="""
examples:
   'foo > 1.5'    will find records where field foo is greater than 1.5, ignoring
                  records where foo is not a number.

    'foo ~ 1\.\d' will find records where field foo is a '1' followed by a decimal point
                  followed by some other digit.
    """, formatter_class=argparse.RawDescriptionHelpFormatter)
    user_parser.add_argument('-e', '--expr', dest='exprs', action='append', default=[],
                             help='Query expression (repeatable). '
                                  'Each record is matched against ALL provided expressions.')
    user_parser.add_argument('-f', '--format', action="store", dest="fmt", default='kvp',
                             help="Output format: kvp (default), json, table")
    user_parser.add_argument('-F', '--fields', action='store', dest='fields', default=None,
                             help="Comma-separated list of fields to snippets, "
                                  "in addition to timestamp and level (default=ALL)")
    user_parser.add_argument('-n', '--page', metavar='ROWS', dest='pagelen', action='store',
                             type=int, default=40,
                             help="For 'table', page length (default=40)")
    user_parser.add_argument('-s', '--shorten', metavar='N', dest='idlen', action='store',
                             type=int, default=36,
                             help="For 'table', shorten identifiers to >= N (default=36)")
    user_parser.add_argument('url', help=url_help)
    user_parser.set_defaults(main_fn=user_main)

    # 'graph' mode
    #TODO finish help text for 'graph'
    graph_parser = subp.add_parser('graph', help="Writes DOT from specified execution log")
    graph_parser.add_argument('url', help=url_help)
    graph_parser.add_argument('-n', '--number', dest='prgm_id', metavar='program_id',
                              help="Program id of the execution to generate a graph for. "
                                   "Defaults to the most recent program id logged.")
    graph_parser.add_argument('-o', '--outdir', dest='out_path', help="Path for output. If no filename is specified, a default will be provided.")
    graph_parser.set_defaults(main_fn=graph_main)

    args = parser.parse_args(cmdline)
    # sanity checks
    parts = parse.urlparse(args.url)
    if not os.path.exists(parts.path):
        parser.error("File for URL path '{}' not found".format(parts.path))
    # set up self-logging
    hndlr = logging.StreamHandler()
    hndlr.setFormatter(logging.Formatter("[%(levelname)s] log.py %(asctime)s %(message)s"))
    g_log.addHandler(hndlr)
    if args.vb > 2:
        g_log.setLevel(logging.DEBUG)
    elif args.vb > 1:
        g_log.setLevel(logging.INFO)
    elif args.vb > 0:
        g_log.setLevel(logging.WARN)
    else:
        g_log.setLevel(logging.ERROR)
    # initialize monitoring lib
    log.init(args.url, readonly=True)
    # run appropriate commands for mode
    return args.main_fn(args)


if __name__ == '__main__':
    sys.exit(main())
