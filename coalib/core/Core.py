import asyncio
import concurrent.futures
import functools
from itertools import groupby
import logging
import multiprocessing

from coalib.collecting.Collectors import collect_files
from coalib.core.DependencyTracker import DependencyTracker
from coalib.core.Graphs import traverse_graph
from coalib.settings.Setting import glob_list


# TODO more loggin messages?

def get_cpu_count():
    try:
        return multiprocessing.cpu_count()
    except NotImplementedError:  # pragma: no cover
        # cpu_count is not implemented for some CPU architectures/OSes
        return 1


def schedule_bears(bears,
                   result_callback,
                   dependency_tracker,
                   event_loop,
                   running_tasks,
                   executor):
    """
    Schedules the tasks of bears to the given executor and runs them on the
    given event loop.

    :param bears:
        A list of bear instances to be scheduled onto the process pool.
    :param result_callback:
        A callback function which is called when results are available.
    :param dependency_tracker:
        The object that keeps track of dependencies.
    :param event_loop:
        The asyncio event loop to schedule bear tasks on.
    :param running_tasks:
        Tasks that are already scheduled, organized in a dict with
        bear instances as keys and asyncio-coroutines as values containing
        their scheduled tasks.
    :param executor:
        The executor to which the bear tasks are scheduled.
    """
    for bear in bears:
        if dependency_tracker.get_dependencies(bear):
            logging.warning(
                "Dependencies for '{}' not yet resolved, holding back. This "
                "should not happen, the dependency tracking system should be "
                "smarter.".format(bear.name))
            # TODO More information? like section instance and name?
        else:
            tasks = {
                event_loop.run_in_executor(
                    executor, bear.execute_task, bear_args, bear_kwargs)
                for bear_args, bear_kwargs in bear.generate_tasks()}

            running_tasks[bear] = tasks

            for task in tasks:
                task.add_done_callback(functools.partial(
                    finish_task, bear, result_callback, dependency_tracker,
                    running_tasks, event_loop, executor))

            logging.debug("Scheduled '{}' (tasks: {}).".format(bear.name,
                                                               len(tasks)))


def finish_task(bear,
                result_callback,
                dependency_tracker,
                running_tasks,
                event_loop,
                executor,
                task):
    """
    The callback for when a task of a bear completes. It is responsible for
    checking if the bear completed its execution and the handling of the
    result generated by the task. It also schedules new tasks if dependencies
    get resolved.

    :param bear:
        The bear that the task belongs to.
    :param result_callback:
        A callback function which is called when results are available.
    :param dependency_tracker:
        The object that keeps track of dependencies.
    :param running_tasks:
        Dictionary that keeps track of the remaining tasks of each bear.
    :param event_loop:
        The ``asyncio`` event loop bear-tasks are scheduled on.
    :param executor:
        The executor to which the bear tasks are scheduled.
    :param task:
        The task that completed.
    """
    # TODO Handle exceptions!!! REALLY IMPORTANT!!! OTHERWISE THIS FREEZES THE
    # TODO   CORE WHEN SOME DO HAPPEN, AS TASKS ARE NOT DELETED ACCORDINGLY
    # TODO   FROM RUNNING TASKS!!

    # FIXME Long operations on the result-callback do block the scheduler
    # FIXME   significantly. It should be possible to schedule new Python
    # FIXME   Threads on the given event_loop and process the callback there.
    try:
        for result in task.result():
            result_callback(result)
    except Exception as ex:
        logging.error('An exception was thrown during bear execution or '
                      'result-handling.', exc_info=ex)
    finally:
        running_tasks[bear].remove(task)
        if not running_tasks[bear]:
            resolved_bears = dependency_tracker.resolve(bear)

            if resolved_bears:
                schedule_bears(resolved_bears, result_callback,
                               dependency_tracker, event_loop, running_tasks,
                               executor)

            del running_tasks[bear]

        if not running_tasks:
            event_loop.stop()


def get_filenames_from_section(section):
    """
    Returns all filenames that are requested for analysis in the given
    ``section``.

    :param section:
        The section to load the filenames from.
    :return:
        An iterable of filenames.
    """
    # TODO Deprecate log-printer on collect_files
    return collect_files(
        glob_list(section.get('files', '')),
        None,
        ignored_file_paths=glob_list(section.get('ignore', '')),
        limit_file_paths=glob_list(section.get('limit_files', '')))


# TODO Test this. Especially with multi-section setup.
# TODO OKAY! This has to be core independent, the user is responsible for
# TODO   correctly initializing bears. As this also allows for virtual
# TODO   files or improves speed for plugins, these could grab the already
# TODO   loaded contents from RAM instead of reloading them from file.
def load_files(bears):
    """
    Loads all files specified in the sections of the bears and arranges them
    inside a file-dictionary, where the keys are the filenames and the values
    the contents of the file (line-split including return characters).

    Files that fail to load are ignored and emit a log-warning.

    :param bears:
        The bears to load the specified files from.
    :return:
        A dictionary containing as keys the section instances mapping to the
        according file-dictionary, which contains filenames as keys and maps
        to the according file-contents.
    """
    section_to_file_dict = {}
    master_file_dict = {}
    # Use this list to not load corrupt/erroring files twice, as this produces
    # doubled log messages.
    corrupt_files = set()

    for section, bears_per_section in groupby(bears,
                                              key=lambda bear: bear.section):
        filenames = get_filenames_from_section(section)

        file_dict = {}
        for filename in filenames:
            try:
                if filename in master_file_dict:
                    file_dict[filename] = master_file_dict[filename]
                elif filename in corrupt_files:
                    # Ignore corrupt files that were already tried to load.
                    pass
                else:
                    with open(filename, 'r', encoding='utf-8') as fl:
                        lines = tuple(fl.readlines())
                    file_dict[filename] = lines
                    master_file_dict[filename] = lines
            except UnicodeDecodeError:
                logging.warning(
                    "Failed to read file '{}'. It seems to contain non-"
                    'unicode characters. Leaving it out.'.format(filename))
                corrupt_files.add(filename)
            except OSError as ex:  # pragma: no cover
                logging.warning(
                    "Failed to read file '{}' because of an unknown error. "
                    'Leaving it out.'.format(filename), exc_info=ex)
                corrupt_files.add(filename)

    logging.debug('Following files loaded:\n' + '\n'.join(
        master_file_dict.keys()))

    return section_to_file_dict


def initialize_dependencies(bears):
    """
    Initializes and returns a ``DependencyTracker`` instance together with a
    set of bears ready for scheduling.

    This function acquires, processes and registers bear dependencies
    accordingly using a consumer-based system, where each dependency bear has
    only a single instance per section.

    The bears set returned accounts for bears that have dependencies and
    excludes them accordingly. Dependency bears that have themselves no further
    dependencies are included so the dependency chain can be processed
    correctly.

    :param bears:
        The set of bears to run that serve as an entry-point.
    :return:
        A tuple with ``(dependency_tracker, bears_to_schedule)``.
    """
    # Pre-collect bears in a set as we use them more than once. Especially
    # remove duplicate instances.
    bears = set(bears)

    dependency_tracker = DependencyTracker()

    # For a consumer-based system, we have a situation which can be visualized
    # with a graph:
    #
    # (section1, file_dict1) (section1, file_dict2) (section2, file_dict2)
    #       |       |                  |                      |
    #       V       V                  V                      V
    #     bear1   bear2              bear3                  bear4
    #       |       |                  |                      |
    #       V       V                  |                      |
    #  BearType1  BearType2            -----------------------|
    #       |       |                                         |
    #       |       |                                         V
    #       ---------------------------------------------> BearType3
    #
    # We need to traverse this graph and instantiate dependency bears
    # accordingly, one per section.

    # Group bears by sections and file-dictionaries. These will serve as
    # entry-points for the dependency-instantiation-graph.
    grouping = groupby(bears, key=lambda bear: (bear.section, bear.file_dict))
    for (section, file_dict), bears_per_section in grouping:
        # Pre-collect bears as the iterator only works once.
        bears_per_section = list(bears_per_section)

        # Now traverse each edge of the graph, and instantiate a new dependency
        # bear if not already instantiated. For the entry point bears, we hack
        # in identity-mappings because those are already instances. Also map
        # the types of the instantiated bears to those instances, as if the
        # user already supplied an instance of a dependency, we reuse it
        # accordingly.
        type_to_instance_map = {}
        for bear in bears_per_section:
            type_to_instance_map[bear] = bear
            type_to_instance_map[type(bear)] = bear

        def instantiate_and_track(prev_bear_type, next_bear_type):
            if next_bear_type not in type_to_instance_map:
                type_to_instance_map[next_bear_type] = (
                    next_bear_type(section, file_dict))

            dependency_tracker.add(type_to_instance_map[next_bear_type],
                                   type_to_instance_map[prev_bear_type])

        traverse_graph(bears_per_section,
                       lambda bear: bear.DEPENDENCIES,
                       instantiate_and_track)

    # TODO Part this up into different function?
    # Get all bears that aren't resolved and exclude those from scheduler set.
    bears -= {bear for bear in bears
              if dependency_tracker.get_dependencies(bear)}

    # Get all bears that have no further dependencies and shall be
    # scheduled additionally.
    for dependency in dependency_tracker.get_all_dependencies():
        if not dependency_tracker.get_dependencies(dependency):
            bears.add(dependency)

    return dependency_tracker, bears


# TODO Prototype, use variables for common executors or default executor.
default_executor = concurrent.futures.ProcessPoolExecutor(
    max_workers=get_cpu_count())


def run(bears, result_callback):
    """
    Runs a coala session.

    :param bears:
        The bear instances to run.
    :param result_callback:
        A callback function which is called when results are available. Must
        have following signature::

            def result_callback(result):
                pass
    """
    # FIXME Allow to pass different executors nicely, for example to execute
    # FIXME   coala with less cores, or to schedule jobs on distributed systems
    # FIXME   (for example Mesos).

    # Set up event loop and executor.
    event_loop = asyncio.SelectorEventLoop()
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=get_cpu_count())

    # Initialize dependency tracking.
    # TODO Shall I part up this function into two parts? This is totally easy
    # TODO    though I'm not sure if it makes sense from usage perspective^^
    dependency_tracker, bears_to_schedule = initialize_dependencies(bears)

    # Let's go.
    schedule_bears(bears_to_schedule, result_callback, dependency_tracker,
                   event_loop, {}, executor)
    try:
        event_loop.run_forever()
    finally:
        event_loop.close()