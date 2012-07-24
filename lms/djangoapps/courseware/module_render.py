import json
import logging

from django.conf import settings
from django.http import Http404
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from functools import wraps

from django.contrib.auth.models import User
from xmodule.modulestore.django import modulestore
from mitxmako.shortcuts import render_to_string
from models import StudentModule, StudentModuleCache
from static_replace import replace_urls

log = logging.getLogger("mitx.courseware")


class I4xSystem(object):
    '''
    This is an abstraction such that x_modules can function independent
    of the courseware (e.g. import into other types of courseware, LMS,
    or if we want to have a sandbox server for user-contributed content)

    I4xSystem objects are passed to x_modules to provide access to system
    functionality.

    Note that these functions can be closures over e.g. a django request
    and user, or other environment-specific info.
    '''
    def __init__(self, ajax_url, track_function,
                 get_module, render_template, replace_urls,
                 user=None, filestore=None, xqueue_callback_url=None):
        '''
        Create a closure around the system environment.

        ajax_url - the url where ajax calls to the encapsulating module go.
        xqueue_callback_url - the url where external queueing system (e.g. for grading)
                              returns its response
        track_function - function of (event_type, event), intended for logging
                         or otherwise tracking the event.
                         TODO: Not used, and has inconsistent args in different
                         files.  Update or remove.
        get_module - function that takes (location) and returns a corresponding
                          module instance object.
        render_template - a function that takes (template_file, context), and returns
                          rendered html.
        user - The user to base the random number generator seed off of for this request
        filestore - A filestore ojbect.  Defaults to an instance of OSFS based at
                    settings.DATA_DIR.
        replace_urls - TEMPORARY - A function like static_replace.replace_urls
            that capa_module can use to fix up the static urls in ajax results.
        '''
        self.ajax_url = ajax_url
        self.xqueue_callback_url = xqueue_callback_url
        self.track_function = track_function
        self.filestore = filestore
        self.get_module = get_module
        self.render_template = render_template
        self.exception404 = Http404
        self.DEBUG = settings.DEBUG
        self.seed = user.id if user is not None else 0
        self.replace_urls = replace_urls

    def get(self, attr):
        '''	provide uniform access to attributes (like etree).'''
        return self.__dict__.get(attr)

    def set(self, attr, val):
        '''provide uniform access to attributes (like etree)'''
        self.__dict__[attr] = val

    def __repr__(self):
        return repr(self.__dict__)

    def __str__(self):
        return str(self.__dict__)


def make_track_function(request):
    '''
    Make a tracking function that logs what happened.
    For use in I4xSystem.
    '''
    import track.views

    def f(event_type, event):
        return track.views.server_track(request, event_type, event, page='x_module')
    return f


def grade_histogram(module_id):
    ''' Print out a histogram of grades on a given problem.
        Part of staff member debug info.
    '''
    from django.db import connection
    cursor = connection.cursor()

    q = """SELECT courseware_studentmodule.grade,
                  COUNT(courseware_studentmodule.student_id)
    FROM courseware_studentmodule
    WHERE courseware_studentmodule.module_id=%s
    GROUP BY courseware_studentmodule.grade"""
    # Passing module_id this way prevents sql-injection.
    cursor.execute(q, [module_id])

    grades = list(cursor.fetchall())
    grades.sort(key=lambda x: x[0])          # Add ORDER BY to sql query?
    if len(grades) == 1 and grades[0][0] is None:
        return []
    return grades


def toc_for_course(user, request, course, active_chapter, active_section):
    '''
    Create a table of contents from the module store

    Return format:
    [ {'name': name, 'sections': SECTIONS, 'active': bool}, ... ]

    where SECTIONS is a list
    [ {'name': name, 'format': format, 'due': due, 'active' : bool}, ...]

    active is set for the section and chapter corresponding to the passed
    parameters.  Everything else comes from the xml, or defaults to "".

    chapters with name 'hidden' are skipped.
    '''

    student_module_cache = StudentModuleCache(user, course, depth=2)
    (course, _, _, _) = get_module(user, request, course.location, student_module_cache)

    chapters = list()
    for chapter in course.get_display_items():
        sections = list()
        for section in chapter.get_display_items():

            active = (chapter.metadata.get('display_name') == active_chapter and
                      section.metadata.get('display_name') == active_section)

            sections.append({'name': section.metadata.get('display_name'),
                             'format': section.metadata.get('format', ''),
                             'due': section.metadata.get('due', ''),
                             'active': active})

        chapters.append({'name': chapter.metadata.get('display_name'),
                         'sections': sections,
                         'active': chapter.metadata.get('display_name') == active_chapter})
    return chapters


def get_section(course_module, chapter, section):
    """
    Returns the xmodule descriptor for the name course > chapter > section,
    or None if this doesn't specify a valid section

    course: Course url
    chapter: Chapter name
    section: Section name
    """

    if course_module is None:
        return

    chapter_module = None
    for _chapter in course_module.get_children():
        if _chapter.metadata.get('display_name') == chapter:
            chapter_module = _chapter
            break

    if chapter_module is None:
        return

    section_module = None
    for _section in chapter_module.get_children():
        if _section.metadata.get('display_name') == section:
            section_module = _section
            break

    return section_module


def get_module(user, request, location, student_module_cache, position=None):
    ''' Get an instance of the xmodule class identified by location,
    setting the state based on an existing StudentModule, or creating one if none
    exists.

    Arguments:
      - user                  : current django User
      - request               : current django HTTPrequest
      - location              : A Location-like object identifying the module to load
      - student_module_cache  : a StudentModuleCache
      - position              : extra information from URL for user-specified
                                position within module

    Returns:
      - a tuple (xmodule instance, instance_module, shared_module, module category).
        instance_module is a StudentModule specific to this module for this student
        shared_module is a StudentModule specific to all modules with the same 'shared_state_key' attribute, or None if the module doesn't elect to share state
    '''
    descriptor = modulestore().get_item(location)

    instance_module = student_module_cache.lookup(descriptor.category, descriptor.location.url())
    shared_state_key = getattr(descriptor, 'shared_state_key', None)
    if shared_state_key is not None:
        shared_module = student_module_cache.lookup(descriptor.category, shared_state_key)
    else:
        shared_module = None

    instance_state = instance_module.state if instance_module is not None else None
    shared_state = shared_module.state if shared_module is not None else None

    # Setup system context for module instance
    ajax_url = settings.MITX_ROOT_URL + '/modx/' + descriptor.location.url() + '/'
    xqueue_callback_url = settings.MITX_ROOT_URL + '/xqueue/' + str(user.id) + '/' + descriptor.location.url() + '/'

    def _get_module(location):
        (module, _, _, _) = get_module(user, request, location, student_module_cache, position)
        return module

    # TODO (cpennington): When modules are shared between courses, the static
    # prefix is going to have to be specific to the module, not the directory
    # that the xml was loaded from
    system = I4xSystem(track_function=make_track_function(request),
                       render_template=render_to_string,
                       ajax_url=ajax_url,
                       xqueue_callback_url=xqueue_callback_url,
                       # TODO (cpennington): Figure out how to share info between systems
                       filestore=descriptor.system.resources_fs,
                       get_module=_get_module,
                       user=user,
                       # TODO (cpennington): This should be removed when all html from
                       # a module is coming through get_html and is therefore covered
                       # by the replace_static_urls code below
                       replace_urls=replace_urls,
                       )
    # pass position specified in URL to module through I4xSystem
    system.set('position', position)

    module = descriptor.xmodule_constructor(system)(instance_state, shared_state)

    replace_prefix = module.metadata['data_dir']
    module = replace_static_urls(module, replace_prefix)

    if settings.MITX_FEATURES.get('DISPLAY_HISTOGRAMS_TO_STAFF') and user.is_staff:
        module = add_histogram(module)

    # If StudentModule for this instance wasn't already in the database,
    # and this isn't a guest user, create it.
    if user.is_authenticated():
        if not instance_module:
            instance_module = StudentModule(
                student=user,
                module_type=descriptor.category,
                module_state_key=module.id,
                state=module.get_instance_state(),
                max_grade=module.max_score())
            instance_module.save()
            # Add to cache. The caller and the system context have references
            # to it, so the change persists past the return
            student_module_cache.append(instance_module)
        if not shared_module and shared_state_key is not None:
            shared_module = StudentModule(
                student=user,
                module_type=descriptor.category,
                module_state_key=shared_state_key,
                state=module.get_shared_state())
            shared_module.save()
            student_module_cache.append(shared_module)

    return (module, instance_module, shared_module, descriptor.category)


def replace_static_urls(module, prefix):
    """
    Updates the supplied module with a new get_html function that wraps
    the old get_html function and substitutes urls of the form /static/...
    with urls that are /static/<prefix>/...
    """
    original_get_html = module.get_html

    @wraps(original_get_html)
    def get_html():
        return replace_urls(original_get_html(), staticfiles_prefix=prefix)

    module.get_html = get_html
    return module


def add_histogram(module):
    """
    Updates the supplied module with a new get_html function that wraps
    the output of the old get_html function with additional information
    for admin users only, including a histogram of student answers and the
    definition of the xmodule
    """
    original_get_html = module.get_html

    @wraps(original_get_html)
    def get_html():
        module_id = module.id
        histogram = grade_histogram(module_id)
        render_histogram = len(histogram) > 0

        # TODO: fixme - no filename in module.xml in general (this code block for edx4edx)
        # the following if block is for summer 2012 edX course development; it will change when the CMS comes online
        if settings.MITX_FEATURES.get('DISPLAY_EDIT_LINK') and settings.DEBUG and module_xml.get('filename') is not None:
            # coursename = multicourse_settings.get_coursename_from_request(request)
            # github_url = multicourse_settings.get_course_github_url(coursename)
            fn = module_xml.get('filename')
            if module_xml.tag == 'problem': fn = 'problems/' + fn	 # grrr
            edit_link = (github_url + '/tree/master/' + fn) if github_url is not None else None
            if module_xml.tag == 'problem': edit_link += '.xml'	 # grrr
        else:
            edit_link = False

        # Cast module.definition and module.metadata to dicts so that json can dump them
        # even though they are lazily loaded
        staff_context = {'definition': json.dumps(dict(module.definition), indent=4),
                         'metadata': json.dumps(dict(module.metadata), indent=4),
                         'element_id': module.location.html_id(),
                         'edit_link': edit_link,
                         'histogram': json.dumps(histogram),
                         'render_histogram': render_histogram,
                         'module_content': original_get_html()}
        return render_to_string("staff_problem_info.html", staff_context)

    module.get_html = get_html
    return module


# TODO: TEMPORARY BYPASS OF AUTH!
@csrf_exempt
def xqueue_callback(request, userid, id, dispatch):
    # Parse xqueue response
    get = request.POST.copy()
    try:
        header = json.loads(get.pop('xqueue_header')[0])  # 'dict'
    except Exception as err:
        msg = "Error in xqueue_callback %s: Invalid return format" % err
        raise Exception(msg)

    # Retrieve target StudentModule
    user = User.objects.get(id=userid)

    student_module_cache = StudentModuleCache(user, modulestore().get_item(id))
    instance, instance_module, shared_module, module_type = get_module(request.user, request, id, student_module_cache)

    if instance_module is None:
        log.debug("Couldn't find module '%s' for user '%s'",
                  id, request.user)
        raise Http404

    oldgrade = instance_module.grade
    old_instance_state = instance_module.state

    # Transfer 'queuekey' from xqueue response header to 'get'. This is required to
    #   use the interface defined by 'handle_ajax'
    get.update({'queuekey': header['queuekey']})

    # We go through the "AJAX" path
    #   So far, the only dispatch from xqueue will be 'score_update'
    try:
        ajax_return = instance.handle_ajax(dispatch, get)  # Can ignore the "ajax" return in 'xqueue_callback'
    except:
        log.exception("error processing ajax call")
        raise

    # Save state back to database
    instance_module.state = instance.get_instance_state()
    if instance.get_score():
        instance_module.grade = instance.get_score()['score']
    if instance_module.grade != oldgrade or instance_module.state != old_instance_state:
        instance_module.save()

    return HttpResponse("")


def modx_dispatch(request, dispatch=None, id=None):
    ''' Generic view for extensions. This is where AJAX calls go.

    Arguments:

      - request -- the django request.
      - dispatch -- the command string to pass through to the module's handle_ajax call
           (e.g. 'problem_reset').  If this string contains '?', only pass
           through the part before the first '?'.
      - id -- the module id. Used to look up the XModule instance
    '''
    # ''' (fix emacs broken parsing)

    # If there are arguments, get rid of them
    dispatch, _, _ = dispatch.partition('?')

    student_module_cache = StudentModuleCache(request.user, modulestore().get_item(id))
    instance, instance_module, shared_module, module_type = get_module(request.user, request, id, student_module_cache)

    if instance_module is None:
        log.debug("Couldn't find module '%s' for user '%s'",
                  id, request.user)
        raise Http404

    oldgrade = instance_module.grade
    old_instance_state = instance_module.state
    old_shared_state = shared_module.state if shared_module is not None else None

    # Let the module handle the AJAX
    try:
        ajax_return = instance.handle_ajax(dispatch, request.POST)
    except:
        log.exception("error processing ajax call")
        raise

    # Save the state back to the database
    instance_module.state = instance.get_instance_state()
    if instance.get_score():
        instance_module.grade = instance.get_score()['score']
    if instance_module.grade != oldgrade or instance_module.state != old_instance_state:
        instance_module.save()

    if shared_module is not None:
        shared_module.state = instance.get_shared_state()
        if shared_module.state != old_shared_state:
            shared_module.save()

    # Return whatever the module wanted to return to the client/caller
    return HttpResponse(ajax_return)
