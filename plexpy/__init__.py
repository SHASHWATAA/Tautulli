﻿# This file is part of Tautulli.
#
#  Tautulli is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Tautulli is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Tautulli.  If not, see <http://www.gnu.org/licenses/>.

import os
from queue import Queue
import sqlite3
import sys
import subprocess
import threading
import datetime
import uuid
import json
# Some cut down versions of Python may not include this module and it's not critical for us
try:
    import webbrowser
    no_browser = False
except ImportError:
    no_browser = True

import cherrypy
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
#from UniversalAnalytics import Tracker

from plexpy import activity_handler
from plexpy import common
from plexpy import database
from plexpy import datafactory
from plexpy import libraries
from plexpy import logger
from plexpy import mobile_app
from plexpy import newsletters
from plexpy import newsletter_handler
from plexpy import notification_handler
from plexpy import notifiers
from plexpy import versioncheck
from plexpy.config import Config
from plexpy.servers import plexServer, plexServers
from plexpy.plextv import PlexTV

PROG_DIR = None
FULL_PATH = None

ARGS = None
SIGNAL = None

SYS_PLATFORM = None
SYS_LANGUAGE = None
SYS_ENCODING = None

QUIET = False
VERBOSE = True
DAEMON = False
CREATEPID = False
PIDFILE = None
NOFORK = False
DOCKER = False

SCHED = BackgroundScheduler()
SCHED_LOCK = threading.Lock()
SCHED_LIST = []

NOTIFY_QUEUE = Queue()

INIT_LOCK = threading.Lock()
_INITIALIZED = False
_STARTED = False
_UPDATE = False

DATA_DIR = None

CONFIG = None
CONFIG_FILE = None

DB_FILE = None

INSTALL_TYPE = None
CURRENT_VERSION = None
LATEST_VERSION = None
COMMITS_BEHIND = None
PREV_RELEASE = None
LATEST_RELEASE = None
UPDATE_AVAILABLE = False

UMASK = None

HTTP_PORT = None
HTTP_ROOT = None

DEV = False

PLEX_SERVER_UP = False

TRACKER = None

WIN_SYS_TRAY_ICON = None

SYS_TIMEZONE = None
SYS_UTC_OFFSET = None

PLEXTV = None
PMS_SERVERS = None

def initialize(config_file):

    with INIT_LOCK:

        global CONFIG
        global CONFIG_FILE
        global _INITIALIZED
        global CURRENT_VERSION
        global LATEST_VERSION
        global PREV_RELEASE
        global UMASK
        global _UPDATE

        CONFIG = Config(config_file)
        CONFIG_FILE = config_file

        assert CONFIG is not None

        if _INITIALIZED:
            return False

        if CONFIG.HTTP_PORT < 21 or CONFIG.HTTP_PORT > 65535:
            logger.warn(u"HTTP_PORT out of bounds: 21 < %s < 65535", CONFIG.HTTP_PORT)
            CONFIG.HTTP_PORT = 8181

        if not CONFIG.HTTPS_CERT:
            CONFIG.HTTPS_CERT = os.path.join(DATA_DIR, 'server.crt')
        if not CONFIG.HTTPS_KEY:
            CONFIG.HTTPS_KEY = os.path.join(DATA_DIR, 'server.key')

        CONFIG.LOG_DIR, log_writable = check_folder_writable(
            CONFIG.LOG_DIR, os.path.join(DATA_DIR, 'logs'), 'logs')
        if not log_writable and not QUIET:
            sys.stderr.write("Unable to create the log directory. Logging to screen only.\n")

        # Start the logger, disable console if needed
        logger.initLogger(console=not QUIET, log_dir=CONFIG.LOG_DIR if log_writable else None,
                          verbose=VERBOSE)

        logger.info(u"Starting Tautulli {}".format(
            common.RELEASE
        ))
        logger.info(u"{} {} ({}{})".format(
            common.PLATFORM, common.PLATFORM_RELEASE, common.PLATFORM_VERSION,
            ' - {}'.format(common.PLATFORM_LINUX_DISTRO) if common.PLATFORM_LINUX_DISTRO else ''
        ))
        logger.info(u"{} (UTC{})".format(
            SYS_TIMEZONE, SYS_UTC_OFFSET
        ))
        logger.info(u"Python {}".format(
            sys.version
        ))
        logger.info(u"Program Dir: {}".format(
            PROG_DIR
        ))
        logger.info(u"Config File: {}".format(
            CONFIG_FILE
        ))
        logger.info(u"Database File: {}".format(
            DB_FILE
        ))

        CONFIG.BACKUP_DIR, _ = check_folder_writable(
            CONFIG.BACKUP_DIR, os.path.join(DATA_DIR, 'backups'), 'backups')
        CONFIG.CACHE_DIR, _ = check_folder_writable(
            CONFIG.CACHE_DIR, os.path.join(DATA_DIR, 'cache'), 'cache')
        CONFIG.NEWSLETTER_DIR, _ = check_folder_writable(
            CONFIG.NEWSLETTER_DIR, os.path.join(DATA_DIR, 'newsletters'), 'newsletters')

        # Initialize the database
        logger.info(u"Checking if the database upgrades are required...")
        try:
            dbcheck()
        except Exception as e:
            logger.error(u"Can't connect to the database: %s" % e)

        # Perform upgrades
        logger.info(u"Checking if configuration upgrades are required...")
        try:
            upgrade()
        except Exception as e:
            logger.error(u"Could not perform upgrades: %s" % e)

        # Add notifier configs to logger blacklist
        newsletters.blacklist_logger()
        notifiers.blacklist_logger()
        mobile_app.blacklist_logger()

        # Check if Tautulli has a uuid
        if CONFIG.PMS_UUID == '' or not CONFIG.PMS_UUID:
            logger.debug(u"Generating UUID...")
            CONFIG.PMS_UUID = generate_uuid()
            CONFIG.write()

        # Check if Tautulli has an API key
        if CONFIG.API_KEY == '':
            logger.debug(u"Generating API key...")
            CONFIG.API_KEY = generate_uuid()
            CONFIG.write()

        # Check if Tautulli has a jwt_secret
        if CONFIG.JWT_SECRET == '' or not CONFIG.JWT_SECRET:
            logger.debug(u"Generating JWT secret...")
            CONFIG.JWT_SECRET = generate_uuid()
            CONFIG.write()

        # Get the previous version from the file
        version_lock_file = os.path.join(DATA_DIR, "version.lock")
        prev_version = None
        if os.path.isfile(version_lock_file):
            try:
                with open(version_lock_file, "r") as fp:
                    prev_version = fp.read()
            except IOError as e:
                logger.error(u"Unable to read previous version from file '%s': %s" %
                             (version_lock_file, e))
        else:
            prev_version = 'cfd30996264b7e9fe4ef87f02d1cc52d1ae8bfca'

        # Get the currently installed version. Returns None, 'win32' or the git
        # hash.
        CURRENT_VERSION, CONFIG.GIT_REMOTE, CONFIG.GIT_BRANCH = versioncheck.getVersion()

        # Write current version to a file, so we know which version did work.
        # This allowes one to restore to that version. The idea is that if we
        # arrive here, most parts of Tautulli seem to work.
        if CURRENT_VERSION:
            try:
                with open(version_lock_file, "w") as fp:
                    fp.write(CURRENT_VERSION)
            except IOError as e:
                logger.error(u"Unable to write current version to file '%s': %s" %
                             (version_lock_file, e))

        # Check for new versions
        if CONFIG.CHECK_GITHUB_ON_STARTUP and CONFIG.CHECK_GITHUB:
            try:
                LATEST_VERSION = versioncheck.check_update()
            except:
                logger.exception(u"Unhandled exception")
                LATEST_VERSION = CURRENT_VERSION
        else:
            LATEST_VERSION = CURRENT_VERSION

        # Get the previous release from the file
        release_file = os.path.join(DATA_DIR, "release.lock")
        PREV_RELEASE = common.RELEASE
        if os.path.isfile(release_file):
            try:
                with open(release_file, "r") as fp:
                    PREV_RELEASE = fp.read()
            except IOError as e:
                logger.error(u"Unable to read previous release from file '%s': %s" %
                             (release_file, e))
        elif prev_version == 'cfd30996264b7e9fe4ef87f02d1cc52d1ae8bfca':  # Commit hash for v1.4.25
            PREV_RELEASE = 'v1.4.25'

        # Check if the release was updated
        if common.RELEASE != PREV_RELEASE:
            CONFIG.UPDATE_SHOW_CHANGELOG = 1
            CONFIG.write()
            _UPDATE = True

        # Write current release version to file for update checking
        try:
            with open(release_file, "w") as fp:
                fp.write(common.RELEASE)
        except IOError as e:
            logger.error(u"Unable to write current release to file '%s': %s" %
                         (release_file, e))

        # Store the original umask
        UMASK = os.umask(0)
        os.umask(UMASK)

        _INITIALIZED = True
        return True


def daemonize():
    if threading.activeCount() != 1:
        logger.warn(
            u"There are %r active threads. Daemonizing may cause"
            " strange behavior.",
            threading.enumerate())

    sys.stdout.flush()
    sys.stderr.flush()

    # Do first fork
    try:
        pid = os.fork()  # @UndefinedVariable - only available in UNIX
        if pid != 0:
            sys.exit(0)
    except OSError as e:
        raise RuntimeError("1st fork failed: %s [%d]", e.strerror, e.errno)

    os.setsid()

    # Make sure I can read my own files and shut out others
    prev = os.umask(0)  # @UndefinedVariable - only available in UNIX
    os.umask(prev and int('077', 8))

    # Make the child a session-leader by detaching from the terminal
    try:
        pid = os.fork()  # @UndefinedVariable - only available in UNIX
        if pid != 0:
            sys.exit(0)
    except OSError as e:
        raise RuntimeError("2nd fork failed: %s [%d]", e.strerror, e.errno)

    dev_null = open('/dev/null', 'r')
    os.dup2(dev_null.fileno(), sys.stdin.fileno())

    si = open('/dev/null', "r")
    so = open('/dev/null', "a+")
    se = open('/dev/null', "a+")

    os.dup2(si.fileno(), sys.stdin.fileno())
    os.dup2(so.fileno(), sys.stdout.fileno())
    os.dup2(se.fileno(), sys.stderr.fileno())

    pid = os.getpid()
    logger.info(u"Daemonized to PID: %d", pid)

    if CREATEPID:
        logger.info(u"Writing PID %d to %s", pid, PIDFILE)
        with open(PIDFILE, 'w') as fp:
            fp.write("%s\n" % pid)


def launch_browser(host, port, root):
    if not no_browser:
        if host == '0.0.0.0':
            host = 'localhost'

        if CONFIG.ENABLE_HTTPS:
            protocol = 'https'
        else:
            protocol = 'http'

        try:
            webbrowser.open('%s://%s:%i%s' % (protocol, host, port, root))
        except Exception as e:
            logger.error(u"Could not launch browser: %s" % e)


def win_system_tray():
    from infi.systray import SysTrayIcon

    def tray_open(sysTrayIcon):
        launch_browser(CONFIG.HTTP_HOST, HTTP_PORT, HTTP_ROOT)

    def tray_check_update(sysTrayIcon):
        versioncheck.check_update()

    def tray_update(sysTrayIcon):
        if UPDATE_AVAILABLE:
            SIGNAL = 'update'
        else:
            hover_text = common.PRODUCT + ' - No Update Available'
            WIN_SYS_TRAY_ICON.update(hover_text=hover_text)

    def tray_restart(sysTrayIcon):
        SIGNAL = 'restart'

    def tray_quit(sysTrayIcon):
        SIGNAL = 'shutdown'

    if UPDATE_AVAILABLE:
        icon = os.path.join(PROG_DIR, 'data/interfaces/', CONFIG.INTERFACE, 'images/logo_tray-update.ico')
        hover_text = common.PRODUCT + ' - Update Available!'
    else:
        icon = os.path.join(PROG_DIR, 'data/interfaces/', CONFIG.INTERFACE, 'images/logo_tray.ico')
        hover_text = common.PRODUCT

    menu_options = (
                    ('Open Tautulli', None, tray_open),
                    ('Check for Updates', None, tray_check_update),
                    ('Update', None, tray_update),
                    ('Restart', None, tray_restart),
    )

    logger.info(u"Launching system tray icon.")

    try:
        WIN_SYS_TRAY_ICON = SysTrayIcon(icon, hover_text, menu_options, on_quit=tray_quit, default_menu_index=1)
        WIN_SYS_TRAY_ICON.start()
    except Exception as e:
        logger.error(u"Unable to launch system tray icon: %s." % e)
        WIN_SYS_TRAY_ICON = None


def initialize_scheduler():
    """
    Start the scheduled background tasks. Re-schedule if interval settings changed.
    """
    github_minutes = CONFIG.CHECK_GITHUB_INTERVAL if CONFIG.CHECK_GITHUB_INTERVAL and CONFIG.CHECK_GITHUB else 0
    backup_hours = CONFIG.BACKUP_INTERVAL if 1 <= CONFIG.BACKUP_INTERVAL <= 24 else 6

    global SCHED_LIST
    SCHED_LIST = []
    SCHED_LIST.append({'name': 'Check GitHub for updates',
                       'time': {'hours': 0, 'minutes': github_minutes, 'seconds': 0},
                       'func': versioncheck.check_update,
                       'args': [bool(CONFIG.PLEXPY_AUTO_UPDATE), True],
                       })

    SCHED_LIST.append({'name': 'Backup Tautulli Database',
                       'time': {'hours': backup_hours, 'minutes': 0, 'seconds': 0},
                       'func': database.make_backup,
                       'args': [True, True],
                       })

    SCHED_LIST.append({'name': 'Backup Tautulli Config',
                       'time': {'hours': backup_hours, 'minutes': 0, 'seconds': 0},
                       'func': config.make_backup,
                       'args': [True, True],
                       })

    schedule_joblist(lock=SCHED_LOCK, scheduler=SCHED, jobList=SCHED_LIST)


def schedule_joblist(lock=None, scheduler=None, jobList=None):
    """
    # Process a scheduler joblist
    """
    with lock:
        # Check if scheduler should be started
        start_jobs = not len(scheduler.get_jobs())

        for job in jobList:
            schedule_job(scheduler=scheduler, name=job['name'], func=job['func'], args=job['args'],
                         hours=job['time']['hours'], minutes=job['time']['minutes'], seconds=job['time']['seconds'])

        # Start scheduler
        if start_jobs and len(scheduler.get_jobs()):
            try:
                scheduler.start()
            except Exception as e:
                logger.error(u'%s' % (e))


def schedule_job(scheduler, func, name, hours=0, minutes=0, seconds=0, args=None):
    """
    #Start scheduled job if starting or restarting plexpy.
    #Reschedule job if Interval Settings have changed.
    #Remove job if Interval Settings changed to 0
    """
    job = scheduler.get_job(name)
    if job:
        if hours == 0 and minutes == 0 and seconds == 0:
            scheduler.remove_job(name)
            logger.info(u"Removed background task: %s", name)
        elif job.trigger.interval != datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds):
            scheduler.reschedule_job(name, trigger=IntervalTrigger(
                hours=hours, minutes=minutes, seconds=seconds), args=args)
            logger.info(u"Re-scheduled background task: %s", name)
    elif hours > 0 or minutes > 0 or seconds > 0:
        scheduler.add_job(func, id=name, trigger=IntervalTrigger(
            hours=hours, minutes=minutes, seconds=seconds), args=args)
        logger.info(u"Scheduled background task: %s", name)


def start():
    global _STARTED
    global PMS_SERVERS
    global PLEXTV

    if _INITIALIZED:
        initialize_scheduler()
        # Start the scheduler for stale stream callbacks
        activity_handler.ACTIVITY_SCHED.start()

        # Start background notification thread
        notification_handler.start_threads(num_threads=CONFIG.NOTIFICATION_THREADS)
        notifiers.check_browser_enabled()

        # Initialize the list of plexServers and Start the monitoring threads
        if CONFIG.PMS_TOKEN:
            PLEXTV = PlexTV()
            PMS_SERVERS = plexServers()
            PMS_SERVERS.start()

        # TODO: JLN - Handle this
        # Initialize System Analytics
        # if CONFIG.SYSTEM_ANALYTICS:
        #     global TRACKER
        #     TRACKER = initialize_tracker()
        #
        #     # Send system analytics events
        #     if not CONFIG.FIRST_RUN_COMPLETE:
        #         analytics_event(category='system', action='install')
        #
        #     elif _UPDATE:
        #         analytics_event(category='system', action='update')
        #
        #     analytics_event(category='system', action='start')
        #
        # Schedule newsletters
        newsletter_handler.NEWSLETTER_SCHED.start()
        newsletter_handler.schedule_newsletters()
        #
        _STARTED = True

def sig_handler(signum=None, frame=None):
    if signum is not None:
        logger.info(u"Signal %i caught, saving and exiting...", signum)
        shutdown()


def dbcheck():
    conn_db = sqlite3.connect(DB_FILE)
    c_db = conn_db.cursor()

    # sessions table :: This is a temp table that logs currently active sessions
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, session_key INTEGER, session_id TEXT, '
        'transcode_key TEXT, rating_key INTEGER, server_id INTEGER, section_id INTEGER, media_type TEXT, started INTEGER, stopped INTEGER, '
        'paused_counter INTEGER DEFAULT 0, state TEXT, user_id INTEGER, user TEXT, friendly_name TEXT, '
        'ip_address TEXT, machine_id TEXT, player TEXT, product TEXT, platform TEXT, title TEXT, parent_title TEXT, '
        'grandparent_title TEXT, original_title TEXT, full_title TEXT, '
        'media_index INTEGER, parent_media_index INTEGER, '
        'thumb TEXT, parent_thumb TEXT, grandparent_thumb TEXT, year INTEGER, '
        'parent_rating_key INTEGER, grandparent_rating_key INTEGER, '
        'view_offset INTEGER DEFAULT 0, duration INTEGER, video_decision TEXT, audio_decision TEXT, '
        'transcode_decision TEXT, container TEXT, bitrate INTEGER, width INTEGER, height INTEGER, '
        'video_codec TEXT, video_bitrate INTEGER, video_resolution TEXT, video_width INTEGER, video_height INTEGER, '
        'video_framerate TEXT, aspect_ratio TEXT, '
        'audio_codec TEXT, audio_bitrate INTEGER, audio_channels INTEGER, subtitle_codec TEXT, '
        'stream_bitrate INTEGER, stream_video_resolution TEXT, quality_profile TEXT, '
        'stream_container_decision TEXT, stream_container TEXT, '
        'stream_video_decision TEXT, stream_video_codec TEXT, stream_video_bitrate INTEGER, stream_video_width INTEGER, '
        'stream_video_height INTEGER, stream_video_framerate TEXT, '
        'stream_audio_decision TEXT, stream_audio_codec TEXT, stream_audio_bitrate INTEGER, stream_audio_channels INTEGER, '
        'subtitles INTEGER, stream_subtitle_decision TEXT, stream_subtitle_codec TEXT, '
        'transcode_protocol TEXT, transcode_container TEXT, '
        'transcode_video_codec TEXT, transcode_audio_codec TEXT, transcode_audio_channels INTEGER,'
        'transcode_width INTEGER, transcode_height INTEGER, '
        'transcode_hw_decoding INTEGER, transcode_hw_encoding INTEGER, '
        'optimized_version INTEGER, optimized_version_profile TEXT, optimized_version_title TEXT, '
        'synced_version INTEGER, synced_version_profile TEXT, '
        'live INTEGER, live_uuid TEXT, '
        'buffer_count INTEGER DEFAULT 0, buffer_last_triggered INTEGER, last_paused INTEGER, watched INTEGER DEFAULT 0, '
        'write_attempts INTEGER DEFAULT 0, raw_stream_info TEXT)'
    )

    # session_history table :: This is a history table which logs essential stream details
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS session_history (id INTEGER PRIMARY KEY AUTOINCREMENT, reference_id INTEGER, '
        'started INTEGER, stopped INTEGER, server_id INTEGER, rating_key INTEGER, user_id INTEGER, user TEXT, '
        'ip_address TEXT, paused_counter INTEGER DEFAULT 0, player TEXT, product TEXT, product_version TEXT, platform TEXT, '
        'platform_version TEXT, profile TEXT, machine_id TEXT, '
        'bandwidth INTEGER, location TEXT, quality_profile TEXT, '
        'parent_rating_key INTEGER, grandparent_rating_key INTEGER, media_type TEXT, view_offset INTEGER DEFAULT 0)'
    )

    # session_history_media_info table :: This is a table which logs each session's media info
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS session_history_media_info (id INTEGER PRIMARY KEY, server_id INTEGER, rating_key INTEGER, '
        'video_decision TEXT, audio_decision TEXT, transcode_decision TEXT, duration INTEGER DEFAULT 0, '
        'container TEXT, bitrate INTEGER, width INTEGER, height INTEGER, video_bitrate INTEGER, video_bit_depth INTEGER, '
        'video_codec TEXT, video_codec_level TEXT, video_width INTEGER, video_height INTEGER, video_resolution TEXT, '
        'video_framerate TEXT, aspect_ratio TEXT, '
        'audio_bitrate INTEGER, audio_codec TEXT, audio_channels INTEGER, transcode_protocol TEXT, '
        'transcode_container TEXT, transcode_video_codec TEXT, transcode_audio_codec TEXT, '
        'transcode_audio_channels INTEGER, transcode_width INTEGER, transcode_height INTEGER, '
        'transcode_hw_requested INTEGER, transcode_hw_full_pipeline INTEGER, '
        'transcode_hw_decode TEXT, transcode_hw_decode_title TEXT, transcode_hw_decoding INTEGER, '
        'transcode_hw_encode TEXT, transcode_hw_encode_title TEXT, transcode_hw_encoding INTEGER, '
        'stream_container TEXT, stream_container_decision TEXT, stream_bitrate INTEGER, '
        'stream_video_decision TEXT, stream_video_bitrate INTEGER, stream_video_codec TEXT, stream_video_codec_level TEXT, '
        'stream_video_bit_depth INTEGER, stream_video_height INTEGER, stream_video_width INTEGER, stream_video_resolution TEXT, '
        'stream_video_framerate TEXT, '
        'stream_audio_decision TEXT, stream_audio_codec TEXT, stream_audio_bitrate INTEGER, stream_audio_channels INTEGER, '
        'stream_subtitle_decision TEXT, stream_subtitle_codec TEXT, stream_subtitle_container TEXT, stream_subtitle_forced INTEGER, '
        'subtitles INTEGER, subtitle_codec TEXT, synced_version INTEGER, synced_version_profile TEXT, '
        'optimized_version INTEGER, optimized_version_profile TEXT, optimized_version_title TEXT)'
    )

    # session_history_metadata table :: This is a table which logs each session's media metadata
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS session_history_metadata (id INTEGER PRIMARY KEY, '
        'rating_key INTEGER, parent_rating_key INTEGER, grandparent_rating_key INTEGER, '
        'title TEXT, parent_title TEXT, grandparent_title TEXT, original_title TEXT, full_title TEXT, '
        'media_index INTEGER, parent_media_index INTEGER, server_id INTEGER, section_id INTEGER, library_id INTEGER, '
        'thumb TEXT, parent_thumb TEXT, grandparent_thumb TEXT, '
        'art TEXT, media_type TEXT, year INTEGER, originally_available_at TEXT, added_at INTEGER, updated_at INTEGER, '
        'last_viewed_at INTEGER, content_rating TEXT, summary TEXT, tagline TEXT, rating TEXT, '
        'duration INTEGER DEFAULT 0, guid TEXT, directors TEXT, writers TEXT, actors TEXT, genres TEXT, studio TEXT, '
        'labels TEXT)'
    )

    # users table :: This table keeps record of the friends list
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'user_id INTEGER DEFAULT NULL UNIQUE, username TEXT NOT NULL, friendly_name TEXT, '
        'thumb TEXT, custom_avatar_url TEXT, email TEXT, is_admin INTEGER DEFAULT 0, is_home_user INTEGER DEFAULT NULL, '
        'is_allow_sync INTEGER DEFAULT NULL, is_restricted INTEGER DEFAULT NULL, do_notify INTEGER DEFAULT 1, '
        'keep_history INTEGER DEFAULT 1, deleted_user INTEGER DEFAULT 0, allow_guest INTEGER DEFAULT 0, '
        'filter_all TEXT, filter_movies TEXT, filter_tv TEXT, filter_music TEXT, filter_photos TEXT '
        ') '
    )

    # user_shared_libraries table :: This table maps a user's per server settings
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS user_shared_libraries ('
        'id INTEGER, '
        'server_id INTEGER, '
        'shared_libraries TEXT, '
        'user_token TEXT, '
        'server_token TEXT, '
        'PRIMARY KEY (id, server_id)'
        ') '
    )

    # library_sections table :: This table keeps record of the servers library sections
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS library_sections (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'server_id INTEGER, section_id INTEGER, section_name TEXT, section_type TEXT, agent TEXT, '
        'thumb TEXT, custom_thumb_url TEXT, art TEXT, count INTEGER, parent_count INTEGER, child_count INTEGER, '
        'do_notify INTEGER DEFAULT 1, do_notify_created INTEGER DEFAULT 1, keep_history INTEGER DEFAULT 1, '
        'deleted_section INTEGER DEFAULT 0, UNIQUE(server_id, section_id))'
    )

    # user_login table :: This table keeps record of the Tautulli guest logins
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS user_login (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'timestamp INTEGER, user_id INTEGER, user TEXT, user_group TEXT, '
        'ip_address TEXT, host TEXT, user_agent TEXT, success INTEGER DEFAULT 1)'
    )

    # notifiers table :: This table keeps record of the notification agent settings
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS notifiers (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'agent_id INTEGER, agent_name TEXT, agent_label TEXT, friendly_name TEXT, notifier_config TEXT, '
        'on_play INTEGER DEFAULT 0, on_stop INTEGER DEFAULT 0, on_pause INTEGER DEFAULT 0, '
        'on_resume INTEGER DEFAULT 0, on_change INTEGER DEFAULT 0, on_buffer INTEGER DEFAULT 0, on_watched INTEGER DEFAULT 0, '
        'on_created INTEGER DEFAULT 0, on_extdown INTEGER DEFAULT 0, on_intdown INTEGER DEFAULT 0, '
        'on_extup INTEGER DEFAULT 0, on_intup INTEGER DEFAULT 0, on_pmsupdate INTEGER DEFAULT 0, '
        'on_concurrent INTEGER DEFAULT 0, on_newdevice INTEGER DEFAULT 0, on_plexpyupdate INTEGER DEFAULT 0, '
        'on_rclonedown INTEGER DEFAULT 0, on_rcloneup INTEGER DEFAULT 0, '
        'on_play_subject TEXT, on_stop_subject TEXT, on_pause_subject TEXT, '
        'on_resume_subject TEXT, on_change_subject TEXT, on_buffer_subject TEXT, on_watched_subject TEXT, '
        'on_created_subject TEXT, on_extdown_subject TEXT, on_intdown_subject TEXT, '
        'on_extup_subject TEXT, on_intup_subject TEXT, on_pmsupdate_subject TEXT, '
        'on_concurrent_subject TEXT, on_newdevice_subject TEXT, on_plexpyupdate_subject TEXT, '
        'on_rclonedown_subject TEXT, on_rcloneup_subject TEXT, '
        'on_play_body TEXT, on_stop_body TEXT, on_pause_body TEXT, '
        'on_resume_body TEXT, on_change_body TEXT, on_buffer_body TEXT, on_watched_body TEXT, '
        'on_created_body TEXT, on_extdown_body TEXT, on_intdown_body TEXT, '
        'on_extup_body TEXT, on_intup_body TEXT, on_pmsupdate_body TEXT, '
        'on_concurrent_body TEXT, on_newdevice_body TEXT, on_plexpyupdate_body TEXT, '
        'on_rclonedown_body TEXT, on_rcloneup_body TEXT, '
        'custom_conditions TEXT, custom_conditions_logic TEXT)'
    )

    # notify_log table :: This is a table which logs notifications sent
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS notify_log (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, '
        'session_key INTEGER, rating_key INTEGER, parent_rating_key INTEGER, grandparent_rating_key INTEGER, '
        'user_id INTEGER, user TEXT, notifier_id INTEGER, agent_id INTEGER, agent_name TEXT, notify_action TEXT, '
        'subject_text TEXT, body_text TEXT, script_args TEXT, success INTEGER DEFAULT 0)'
    )

    # newsletters table :: This table keeps record of the newsletter settings
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS newsletters (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'agent_id INTEGER, agent_name TEXT, agent_label TEXT, id_name TEXT NOT NULL DEFAULT "", '
        'friendly_name TEXT, newsletter_config TEXT, email_config TEXT, '
        'subject TEXT, body TEXT, message TEXT, '
        'cron TEXT NOT NULL DEFAULT "0 0 * * 0", active INTEGER DEFAULT 0)'
    )

    # newsletter_log table :: This is a table which logs newsletters sent
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS newsletter_log (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, '
        'newsletter_id INTEGER, agent_id INTEGER, agent_name TEXT, notify_action TEXT, '
        'subject_text TEXT, body_text TEXT, message_text TEXT, start_date TEXT, end_date TEXT, '
        'start_time INTEGER, end_time INTEGER, uuid TEXT UNIQUE, filename TEXT, email_msg_id TEXT, '
        'success INTEGER DEFAULT 0)'
    )

    # recently_added table :: This table keeps record of recently added items
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS recently_added (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'added_at INTEGER, server_id INTEGER, section_id INTEGER, '
        'rating_key INTEGER, parent_rating_key INTEGER, grandparent_rating_key INTEGER, media_type TEXT, '
        'media_info TEXT)'
    )

    # mobile_devices table :: This table keeps record of devices linked with the mobile app
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS mobile_devices (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'device_id TEXT NOT NULL UNIQUE, device_token TEXT, device_name TEXT, friendly_name TEXT, '
        'last_seen INTEGER)'
    )

    # tvmaze_lookup table :: This table keeps record of the TVmaze lookups
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS tvmaze_lookup (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'server_id INTEGER, rating_key INTEGER, thetvdb_id INTEGER, imdb_id TEXT, '
        'tvmaze_id INTEGER, tvmaze_url TEXT, tvmaze_json TEXT)'
    )

    # themoviedb_lookup table :: This table keeps record of the TheMovieDB lookups
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS themoviedb_lookup (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'server_id INTEGER, rating_key INTEGER, thetvdb_id INTEGER, imdb_id TEXT, '
        'themoviedb_id INTEGER, themoviedb_url TEXT, themoviedb_json TEXT)'
    )

    # image_hash_lookup table :: This table keeps record of the image hash lookups
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS image_hash_lookup (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'img_hash TEXT, img TEXT, server_id INTEGER, rating_key INTEGER, width INTEGER, height INTEGER, '
        'opacity INTEGER, background TEXT, blur INTEGER, fallback TEXT)'
    )

    # imgur_lookup table :: This table keeps record of the Imgur uploads
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS imgur_lookup (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'img_hash TEXT, imgur_title TEXT, imgur_url TEXT, delete_hash TEXT)'
    )

    # cloudinary_lookup table :: This table keeps record of the Cloudinary uploads
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS cloudinary_lookup (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'img_hash TEXT, cloudinary_title TEXT, cloudinary_url TEXT)'
    )

    # Servers table :: This table is a list of the configured servers
    c_db.execute(
        'CREATE TABLE IF NOT EXISTS servers ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'pms_name TEXT, '
        'pms_ip TEXT, '
        'pms_port INTEGER, '
        'pms_identifier TEXT, '
        'pms_token TEXT, '
        'pms_is_enabled BOOL DEFAULT 0, '
        'pms_is_deleted BOOL DEFAULT 0, '
        'pms_is_remote BOOL DEFAULT 0, '
        'pms_is_cloud BOOL DEFAULT 0, '
        'pms_ssl BOOL DEFAULT 0, '
        'pms_ssl_pref INT DEFAULT 1, '
        'pms_url TEXT, '
        'pms_uri TEXT, '
        'pms_url_manual BOOL DEFAULT 0, '
        'pms_url_override TEXT, '
        'pms_web_url TEXT DEFAULT "https://app.plex.tv/desktop", '
        'pms_use_bif BOOL DEFAULT 0, '
        'pms_update_channel TEXT DEFAULT "plex", '
        'pms_version TEXT, '
        'pms_platform TEXT, '
        'pms_update_distro TEXT, '
        'pms_update_distro_build TEXT, '
        'monitor_pms_updates BOOL DEFAULT 0, '
        'monitor_remote_access BOOL DEFAULT 0, '
        'refresh_libraries_on_startup BOOL DEFAULT 1, '
        'refresh_libraries_interval INT DEFAULT 12, '
        'monitor_rclone_mount BOOL DEFAULT 0, '
        'rclone_user TEXT DEFAULT "", '
        'rclone_pass TEXT DEFAULT "", '
        'rclone_mountdir TEXT DEFAULT "", '
        'rclone_tmpdir TEXT DEFAULT "", '
        'rclone_testfile TEXT DEFAULT "", '
        'rclone_port INT DEFAULT 5572, '
        'rclone_ssl INT DEFAULT 0, '
        'rclone_ssl_hostname TEXT DEFAULT "" '
        ')'
    )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT started FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN started INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN paused_counter INTEGER DEFAULT 0'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN state TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN user TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN machine_id TEXT'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT title FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN title TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN parent_title TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN grandparent_title TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN friendly_name TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN player TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN user_id INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT ip_address FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN ip_address TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN platform TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN parent_rating_key INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN grandparent_rating_key INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN view_offset INTEGER DEFAULT 0'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN duration INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN video_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN audio_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN width INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN height INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN container TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN video_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN audio_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN video_resolution TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN video_framerate TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN aspect_ratio TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN audio_channels INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_protocol TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_container TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_video_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_audio_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_audio_channels INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_width INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_height INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT buffer_count FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN buffer_count INTEGER DEFAULT 0'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN buffer_last_triggered INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT last_paused FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN last_paused INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT section_id FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN section_id INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT stopped FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stopped INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT transcode_key FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_key TEXT'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT write_attempts FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN write_attempts INTEGER DEFAULT 0'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT transcode_decision FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN full_title TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN media_index INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN parent_media_index INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN thumb TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN parent_thumb TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN grandparent_thumb TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN year INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT raw_stream_info FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN product TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN optimized_version INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN optimized_version_profile TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN synced_version INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN video_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN video_width INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN video_height INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN audio_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN subtitle_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_video_resolution TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN quality_profile TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_container_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_container TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_video_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_video_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_video_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_video_width INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_video_height INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_video_framerate TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_audio_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_audio_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_audio_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_audio_channels INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN subtitles INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_subtitle_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN stream_subtitle_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN raw_stream_info TEXT'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT video_height FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN video_height INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT subtitles FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN subtitles INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT synced_version_profile FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN synced_version_profile TEXT'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN optimized_version_title TEXT'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT transcode_hw_decoding FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_hw_decoding INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN transcode_hw_encoding INTEGER'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT watched FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN watched INTEGER DEFAULT 0'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT live FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN live INTEGER'
        )
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN live_uuid TEXT'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT session_id FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN session_id TEXT'
        )

    # Upgrade sessions table from earlier versions
    try:
        c_db.execute('SELECT original_title FROM sessions')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table sessions.")
        c_db.execute(
            'ALTER TABLE sessions ADD COLUMN original_title TEXT'
        )

    # Upgrade session_history table from earlier versions
    try:
        c_db.execute('SELECT reference_id FROM session_history')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history.")
        c_db.execute(
            'ALTER TABLE session_history ADD COLUMN reference_id INTEGER DEFAULT 0'
        )
        # Set reference_id to the first row where (user_id = previous row, rating_key != previous row) and user_id = user_id
        c_db.execute(
            'UPDATE session_history ' \
            'SET reference_id = (SELECT (CASE \
             WHEN (SELECT MIN(id) FROM session_history WHERE id > ( \
                 SELECT MAX(id) FROM session_history \
                 WHERE (user_id = t1.user_id AND rating_key <> t1.rating_key AND id < t1.id)) AND user_id = t1.user_id) IS NULL \
             THEN (SELECT MIN(id) FROM session_history WHERE (user_id = t1.user_id)) \
             ELSE (SELECT MIN(id) FROM session_history WHERE id > ( \
                 SELECT MAX(id) FROM session_history \
                 WHERE (user_id = t1.user_id AND rating_key <> t1.rating_key AND id < t1.id)) AND user_id = t1.user_id) END) ' \
            'FROM session_history AS t1 ' \
            'WHERE t1.id = session_history.id) '
        )

    # Upgrade session_history table from earlier versions
    try:
        c_db.execute('SELECT bandwidth FROM session_history')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history.")
        c_db.execute(
            'ALTER TABLE session_history ADD COLUMN platform_version TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history ADD COLUMN product TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history ADD COLUMN product_version TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history ADD COLUMN profile TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history ADD COLUMN bandwidth INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history ADD COLUMN location TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history ADD COLUMN quality_profile TEXT'
        )

    # Upgrade session_history_metadata table from earlier versions
    try:
        c_db.execute('SELECT full_title FROM session_history_metadata')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_metadata.")
        c_db.execute(
            'ALTER TABLE session_history_metadata ADD COLUMN full_title TEXT'
        )

    # Upgrade session_history_metadata table from earlier versions
    try:
        c_db.execute('SELECT tagline FROM session_history_metadata')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_metadata.")
        c_db.execute(
            'ALTER TABLE session_history_metadata ADD COLUMN tagline TEXT'
        )

    # Upgrade session_history_metadata table from earlier versions
    try:
        c_db.execute('SELECT section_id FROM session_history_metadata')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_metadata.")
        c_db.execute(
            'ALTER TABLE session_history_metadata ADD COLUMN section_id INTEGER'
        )

    # Upgrade session_history_metadata table from earlier versions
    try:
        c_db.execute('SELECT labels FROM session_history_metadata')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_metadata.")
        c_db.execute(
            'ALTER TABLE session_history_metadata ADD COLUMN labels TEXT'
        )

    # Upgrade session_history_metadata table from earlier versions
    try:
        c_db.execute('SELECT original_title FROM session_history_metadata')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_metadata.")
        c_db.execute(
            'ALTER TABLE session_history_metadata ADD COLUMN original_title TEXT'
        )

    # Upgrade session_history_media_info table from earlier versions
    try:
        c_db.execute('SELECT transcode_decision FROM session_history_media_info')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_media_info.")
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_decision TEXT'
        )
        c_db.execute(
            'UPDATE session_history_media_info SET transcode_decision = (CASE '
            'WHEN video_decision = "transcode" OR audio_decision = "transcode" THEN "transcode" '
            'WHEN video_decision = "copy" OR audio_decision = "copy" THEN "copy" '
            'WHEN video_decision = "direct play" OR audio_decision = "direct play" THEN "direct play" END)'
        )

    # Upgrade session_history_media_info table from earlier versions
    try:
        c_db.execute('SELECT subtitles FROM session_history_media_info')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_media_info.")
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN video_bit_depth INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN video_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN video_codec_level TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN video_width INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN video_height INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN audio_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_hw_requested INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_hw_full_pipeline INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_hw_decode TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_hw_encode TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_hw_decode_title TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_hw_encode_title TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_container TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_container_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_codec_level TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_bit_depth INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_height INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_width INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_resolution TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_video_framerate TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_audio_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_audio_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_audio_bitrate INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_audio_channels INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_subtitle_decision TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_subtitle_codec TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_subtitle_container TEXT'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN stream_subtitle_forced INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN subtitles INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN synced_version INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN optimized_version INTEGER'
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN optimized_version_profile TEXT'
        )
        c_db.execute(
            'UPDATE session_history_media_info SET video_resolution=REPLACE(video_resolution, "p", "")'
        )
        c_db.execute(
            'UPDATE session_history_media_info SET video_resolution=REPLACE(video_resolution, "SD", "sd")'
        )

    # Upgrade session_history_media_info table from earlier versions
    try:
        c_db.execute('SELECT subtitle_codec FROM session_history_media_info')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_media_info.")
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN subtitle_codec TEXT '
        )

    # Upgrade session_history_media_info table from earlier versions
    try:
        c_db.execute('SELECT synced_version_profile FROM session_history_media_info')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_media_info.")
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN synced_version_profile TEXT '
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN optimized_version_title TEXT '
        )

    # Upgrade session_history_media_info table from earlier versions
    try:
        c_db.execute('SELECT transcode_hw_decoding FROM session_history_media_info')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table session_history_media_info.")
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_hw_decoding INTEGER '
        )
        c_db.execute(
            'ALTER TABLE session_history_media_info ADD COLUMN transcode_hw_encoding INTEGER '
        )
        c_db.execute(
            'UPDATE session_history_media_info SET subtitle_codec = "" WHERE subtitle_codec IS NULL '
        )

    # Upgrade session_history_media_info table from earlier versions
    try:
        result = c_db.execute('SELECT stream_container FROM session_history_media_info '
                              'WHERE stream_container IS NULL').fetchall()
        if len(result) > 0:
            logger.debug(u"Altering database. Removing NULL values from session_history_media_info table.")
            c_db.execute(
                'UPDATE session_history_media_info SET stream_container = "" WHERE stream_container IS NULL '
            )
            c_db.execute(
                'UPDATE session_history_media_info SET stream_video_codec = "" WHERE stream_video_codec IS NULL '
            )
            c_db.execute(
                'UPDATE session_history_media_info SET stream_audio_codec = "" WHERE stream_audio_codec IS NULL '
            )
            c_db.execute(
                'UPDATE session_history_media_info SET stream_subtitle_codec = "" WHERE stream_subtitle_codec IS NULL '
            )
    except sqlite3.OperationalError:
        logger.warn(u"Unable to remove NULL values from session_history_media_info table.")

    # Upgrade users table from earlier versions
    try:
        c_db.execute('SELECT do_notify FROM users')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table users.")
        c_db.execute(
            'ALTER TABLE users ADD COLUMN do_notify INTEGER DEFAULT 1'
        )

    # Upgrade users table from earlier versions
    try:
        c_db.execute('SELECT keep_history FROM users')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table users.")
        c_db.execute(
            'ALTER TABLE users ADD COLUMN keep_history INTEGER DEFAULT 1'
        )

    # Upgrade users table from earlier versions
    try:
        c_db.execute('SELECT custom_avatar_url FROM users')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table users.")
        c_db.execute(
            'ALTER TABLE users ADD COLUMN custom_avatar_url TEXT'
        )

    # Upgrade users table from earlier versions
    try:
        c_db.execute('SELECT deleted_user FROM users')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table users.")
        c_db.execute(
            'ALTER TABLE users ADD COLUMN deleted_user INTEGER DEFAULT 0'
        )

    # Upgrade users table from earlier versions
    try:
        c_db.execute('SELECT allow_guest FROM users')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table users.")
        c_db.execute(
            'ALTER TABLE users ADD COLUMN allow_guest INTEGER DEFAULT 0'
        )
        c_db.execute(
            'ALTER TABLE users ADD COLUMN user_token TEXT'
        )
        c_db.execute(
            'ALTER TABLE users ADD COLUMN server_token TEXT'
        )

    # Upgrade users table from earlier versions
    try:
        c_db.execute('SELECT filter_all FROM users')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table users.")
        c_db.execute(
            'ALTER TABLE users ADD COLUMN filter_all TEXT'
        )
        c_db.execute(
            'ALTER TABLE users ADD COLUMN filter_movies TEXT'
        )
        c_db.execute(
            'ALTER TABLE users ADD COLUMN filter_tv TEXT'
        )
        c_db.execute(
            'ALTER TABLE users ADD COLUMN filter_music TEXT'
        )
        c_db.execute(
            'ALTER TABLE users ADD COLUMN filter_photos TEXT'
        )

    # Upgrade users table from earlier versions
    try:
        c_db.execute('SELECT is_admin FROM users')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table users.")
        c_db.execute(
            'ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0'
        )

    # Upgrade notify_log table from earlier versions
    try:
        c_db.execute('SELECT poster_url FROM notify_log')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table notify_log.")
        c_db.execute(
            'ALTER TABLE notify_log ADD COLUMN poster_url TEXT'
        )

    # Upgrade notify_log table from earlier versions (populate table with data from notify_log)
    try:
        c_db.execute('SELECT timestamp FROM notify_log')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table notify_log.")
        c_db.execute(
            'CREATE TABLE IF NOT EXISTS notify_log_temp (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, '
            'session_key INTEGER, rating_key INTEGER, parent_rating_key INTEGER, grandparent_rating_key INTEGER, '
            'user_id INTEGER, user TEXT, agent_id INTEGER, agent_name TEXT, notify_action TEXT, '
            'subject_text TEXT, body_text TEXT, script_args TEXT, poster_url TEXT)'
        )
        c_db.execute(
            'INSERT INTO notify_log_temp (session_key, rating_key, user_id, user, agent_id, agent_name, '
            'poster_url, timestamp, notify_action) '
            'SELECT session_key, rating_key, user_id, user, agent_id, agent_name, poster_url, timestamp, '
            'notify_action FROM notify_log_temp '
            'UNION ALL SELECT session_key, rating_key, user_id, user, agent_id, agent_name, poster_url, '
            'on_play, "play" FROM notify_log WHERE on_play '
            'UNION ALL SELECT session_key, rating_key, user_id, user, agent_id, agent_name, poster_url, '
            'on_stop, "stop" FROM notify_log WHERE on_stop '
            'UNION ALL SELECT session_key, rating_key, user_id, user, agent_id, agent_name, poster_url, '
            'on_watched, "watched" FROM notify_log WHERE on_watched '
            'UNION ALL SELECT session_key, rating_key, user_id, user, agent_id, agent_name, poster_url, '
            'on_pause, "pause" FROM notify_log WHERE on_pause '
            'UNION ALL SELECT session_key, rating_key, user_id, user, agent_id, agent_name, poster_url, '
            'on_resume, "resume" FROM notify_log WHERE on_resume '
            'UNION ALL SELECT session_key, rating_key, user_id, user, agent_id, agent_name, poster_url, '
            'on_buffer, "buffer" FROM notify_log WHERE on_buffer '
            'UNION ALL SELECT session_key, rating_key, user_id, user, agent_id, agent_name, poster_url, '
            'on_created, "created" FROM notify_log WHERE on_created '
            'ORDER BY timestamp ')
        c_db.execute(
            'DROP TABLE notify_log'
        )
        c_db.execute(
            'ALTER TABLE notify_log_temp RENAME TO notify_log'
        )

    # Upgrade notify_log table from earlier versions
    try:
        c_db.execute('SELECT notifier_id FROM notify_log')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table notify_log.")
        c_db.execute(
            'ALTER TABLE notify_log ADD COLUMN notifier_id INTEGER'
        )

    # Upgrade notify_log table from earlier versions
    try:
        c_db.execute('SELECT success FROM notify_log')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table notify_log.")
        c_db.execute(
            'ALTER TABLE notify_log ADD COLUMN success INTEGER DEFAULT 0'
        )
        c_db.execute(
            'UPDATE notify_log SET success = 1'
        )

    # Upgrade newsletter_log table from earlier versions
    try:
        c_db.execute('SELECT start_time FROM newsletter_log')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table newsletter_log.")
        c_db.execute(
            'ALTER TABLE newsletter_log ADD COLUMN start_time INTEGER'
        )
        c_db.execute(
            'ALTER TABLE newsletter_log ADD COLUMN end_time INTEGER'
        )

    # Upgrade newsletter_log table from earlier versions
    try:
        c_db.execute('SELECT filename FROM newsletter_log')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table newsletter_log.")
        c_db.execute(
            'ALTER TABLE newsletter_log ADD COLUMN filename TEXT'
        )

    # Upgrade newsletter_log table from earlier versions
    try:
        c_db.execute('SELECT email_msg_id FROM newsletter_log')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table newsletter_log.")
        c_db.execute(
            'ALTER TABLE newsletter_log ADD COLUMN email_msg_id TEXT'
        )

    # Upgrade newsletters table from earlier versions
    try:
        c_db.execute('SELECT id_name FROM newsletters')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table newsletters.")
        c_db.execute(
            'ALTER TABLE newsletters ADD COLUMN id_name TEXT NOT NULL DEFAULT ""'
        )

    # Upgrade library_sections table from earlier versions (remove UNIQUE constraint on section_id)
    try:
        result = c_db.execute('SELECT SQL FROM sqlite_master WHERE type="table" AND name="library_sections"').fetchone()
        if 'section_id INTEGER UNIQUE' in result[0]:
            logger.debug(u"Altering database. Removing unique constraint on section_id from library_sections table.")
            c_db.execute(
                'CREATE TABLE library_sections_temp (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'server_id TEXT, section_id INTEGER, section_name TEXT, section_type TEXT, '
                'thumb TEXT, custom_thumb_url TEXT, art TEXT, count INTEGER, parent_count INTEGER, child_count INTEGER, '
                'do_notify INTEGER DEFAULT 1, do_notify_created INTEGER DEFAULT 1, keep_history INTEGER DEFAULT 1, '
                'deleted_section INTEGER DEFAULT 0, UNIQUE(server_id, section_id))'
            )
            c_db.execute(
                'INSERT INTO library_sections_temp (id, server_id, section_id, section_name, section_type, '
                'thumb, custom_thumb_url, art, count, parent_count, child_count, do_notify, do_notify_created, '
                'keep_history, deleted_section) '
                'SELECT id, server_id, section_id, section_name, section_type, '
                'thumb, custom_thumb_url, art, count, parent_count, child_count, do_notify, do_notify_created, '
                'keep_history, deleted_section '
                'FROM library_sections'
            )
            c_db.execute(
                'DROP TABLE library_sections'
            )
            c_db.execute(
                'ALTER TABLE library_sections_temp RENAME TO library_sections'
            )
    except sqlite3.OperationalError:
        logger.warn(u"Unable to remove section_id unique constraint from library_sections.")
        try:
            c_db.execute(
                'DROP TABLE library_sections_temp'
            )
        except:
            pass

    # Upgrade library_sections table from earlier versions (remove duplicated libraries)
    try:
        result = c_db.execute('SELECT * FROM library_sections WHERE server_id = ""').fetchall()
        if len(result) > 0:
            logger.debug(u"Altering database. Removing duplicate libraries from library_sections table.")
            c_db.execute(
                'DELETE FROM library_sections WHERE server_id = ""'
            )
    except sqlite3.OperationalError:
        logger.warn(u"Unable to remove duplicate libraries from library_sections table.")

    # Upgrade library_sections table from earlier versions
    try:
        c_db.execute('SELECT agent FROM library_sections')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table library_sections.")
        c_db.execute(
            'ALTER TABLE library_sections ADD COLUMN agent TEXT'
        )

    # Upgrade users table from earlier versions (remove UNIQUE constraint on username)
    try:
        result = c_db.execute('SELECT SQL FROM sqlite_master WHERE type="table" AND name="users"').fetchone()
        if 'username TEXT NOT NULL UNIQUE' in result[0]:
            logger.debug(u"Altering database. Removing unique constraint on username from users table.")
            c_db.execute(
                'CREATE TABLE users_temp (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'user_id INTEGER DEFAULT NULL UNIQUE, username TEXT NOT NULL, friendly_name TEXT, '
                'thumb TEXT, custom_avatar_url TEXT, email TEXT, is_home_user INTEGER DEFAULT NULL, '
                'is_allow_sync INTEGER DEFAULT NULL, is_restricted INTEGER DEFAULT NULL, do_notify INTEGER DEFAULT 1, '
                'keep_history INTEGER DEFAULT 1, deleted_user INTEGER DEFAULT 0)'
            )
            c_db.execute(
                'INSERT INTO users_temp (id, user_id, username, friendly_name, thumb, custom_avatar_url, '
                'email, is_home_user, is_allow_sync, is_restricted, do_notify, keep_history, deleted_user) '
                'SELECT id, user_id, username, friendly_name, thumb, custom_avatar_url, '
                'email, is_home_user, is_allow_sync, is_restricted, do_notify, keep_history, deleted_user '
                'FROM users'
            )
            c_db.execute(
                'DROP TABLE users'
            )
            c_db.execute(
                'ALTER TABLE users_temp RENAME TO users'
            )
    except sqlite3.OperationalError:
        logger.warn(u"Unable to remove username unique constraint from users.")
        try:
            c_db.execute(
                'DROP TABLE users_temp'
            )
        except:
            pass

    # Upgrade mobile_devices table from earlier versions
    try:
        result = c_db.execute('SELECT SQL FROM sqlite_master WHERE type="table" AND name="mobile_devices"').fetchone()
        if 'device_token TEXT NOT NULL UNIQUE' in result[0]:
            logger.debug(u"Altering database. Dropping and recreating mobile_devices table.")
            c_db.execute(
                'DROP TABLE mobile_devices'
            )
            c_db.execute(
                'CREATE TABLE IF NOT EXISTS mobile_devices (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'device_id TEXT NOT NULL UNIQUE, device_token TEXT, device_name TEXT, friendly_name TEXT)'
            )
    except sqlite3.OperationalError:
        logger.warn(u"Failed to recreate mobile_devices table.")
        pass

    # Upgrade mobile_devices table from earlier versions
    try:
        c_db.execute('SELECT last_seen FROM mobile_devices')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table mobile_devices.")
        c_db.execute(
            'ALTER TABLE mobile_devices ADD COLUMN last_seen INTEGER'
        )

    # Upgrade notifiers table from earlier versions
    try:
        c_db.execute('SELECT custom_conditions FROM notifiers')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table notifiers.")
        c_db.execute(
            'ALTER TABLE notifiers ADD COLUMN custom_conditions TEXT'
        )
        c_db.execute(
            'ALTER TABLE notifiers ADD COLUMN custom_conditions_logic TEXT'
        )

    # Upgrade notifiers table from earlier versions
    try:
        c_db.execute('SELECT on_change FROM notifiers')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table notifiers.")
        c_db.execute(
            'ALTER TABLE notifiers ADD COLUMN on_change INTEGER DEFAULT 0'
        )
        c_db.execute(
            'ALTER TABLE notifiers ADD COLUMN on_change_subject TEXT'
        )
        c_db.execute(
            'ALTER TABLE notifiers ADD COLUMN on_change_body TEXT'
        )

    # Upgrade tvmaze_lookup table from earlier versions
    try:
        c_db.execute('SELECT rating_key FROM tvmaze_lookup')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table tvmaze_lookup.")
        c_db.execute(
            'ALTER TABLE tvmaze_lookup ADD COLUMN rating_key INTEGER'
        )
        c_db.execute(
            'DROP INDEX IF EXISTS idx_tvmaze_lookup_thetvdb_id'
        )
        c_db.execute(
            'DROP INDEX IF EXISTS idx_tvmaze_lookup_imdb_id'
        )

    # Upgrade themoviedb_lookup table from earlier versions
    try:
        c_db.execute('SELECT rating_key FROM themoviedb_lookup')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table themoviedb_lookup.")
        c_db.execute(
            'ALTER TABLE themoviedb_lookup ADD COLUMN rating_key INTEGER'
        )
        c_db.execute(
            'DROP INDEX IF EXISTS idx_themoviedb_lookup_thetvdb_id'
        )
        c_db.execute(
            'DROP INDEX IF EXISTS idx_themoviedb_lookup_imdb_id'
        )

    # Upgrade user_login table from earlier versions
    try:
        c_db.execute('SELECT success FROM user_login')
    except sqlite3.OperationalError:
        logger.debug(u"Altering database. Updating database table user_login.")
        c_db.execute(
            'ALTER TABLE user_login ADD COLUMN success INTEGER DEFAULT 1'
        )

    # Rename notifiers in the database
    result = c_db.execute('SELECT agent_label FROM notifiers '
                          'WHERE agent_label = "XBMC" OR agent_label = "OSX Notify"').fetchone()
    if result:
        logger.debug(u"Altering database. Renaming notifiers.")
        c_db.execute(
            'UPDATE notifiers SET agent_label = "Kodi" WHERE agent_label = "XBMC"'
        )
        c_db.execute(
            'UPDATE notifiers SET agent_label = "macOS Notification Center" WHERE agent_label = "OSX Notify"'
        )

    # Add "Local" user to database as default unauthenticated user.
    result = c_db.execute('SELECT id FROM users WHERE username = "Local"')
    if not result.fetchone():
        logger.debug(u"User 'Local' does not exist. Adding user.")
        c_db.execute('INSERT INTO users (user_id, username) VALUES (0, "Local")')

    # Create table indices
    c_db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_tvmaze_lookup ON tvmaze_lookup (rating_key)'
    )
    c_db.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_themoviedb_lookup ON themoviedb_lookup (rating_key)'
    )

    # Create servers table and migrate library_sections and session_history_metadata
    try:
        result = c_db.execute('PRAGMA TABLE_INFO(library_sections)').fetchall()
        doV3Migration = False
        for row in result:
            if row[1] == 'server_id':
                if row[2] == 'TEXT':
                    doV3Migration = True
                break

        if doV3Migration:
            logger.debug(u"Multi-Server Migration - Converting Database to Multi-Server Format.")
            result = c_db.execute('SELECT SQL FROM sqlite_master WHERE type="table" AND name="library_sections"').fetchone()
            library_sections_temp = result[0].replace("server_id TEXT", "server_id INTEGER").replace("library_sections", "library_sections_temp")

            logger.debug(u"Multi-Server Migration - Inserting configured server into servers table.")
            from configobj import ConfigObj
            config = ConfigObj(CONFIG_FILE, encoding='utf-8')

            c_db.execute(
                'INSERT INTO servers (pms_name, pms_ip, pms_port, '
                'pms_is_remote, pms_ssl, pms_url, pms_uri, pms_token, pms_identifier, '
                'pms_url_manual, pms_web_url, '
                'pms_use_bif, pms_update_channel, '
                'pms_update_distro, pms_update_distro_build, pms_url_override, '
                'pms_version, pms_platform, pms_is_cloud, pms_is_enabled, '
                'monitor_pms_updates, monitor_remote_access, '
                'refresh_libraries_on_startup, refresh_libraries_interval '
                ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (config['PMS']['pms_name'], config['PMS']['pms_ip'], config['PMS']['pms_port'],
                 bool(int(config['PMS']['pms_is_remote'])), bool(int(config['PMS']['pms_ssl'])),
                 config['PMS']['pms_url'], config['PMS']['pms_url'], config['PMS']['pms_token'], config['PMS']['pms_identifier'],
                 bool(int(config['PMS']['pms_url_manual'])), config['PMS']['pms_web_url'],
                 bool(int(config['PMS']['pms_use_bif'])), config['PMS']['pms_update_channel'],
                 config['PMS']['pms_update_distro'], config['PMS']['pms_update_distro_build'], config['PMS']['pms_url_override'],
                 config['PMS']['pms_version'],
                 config['PMS']['pms_platform'], bool(int(config['PMS']['pms_is_cloud'])), 1,
                 config['Monitoring']['monitor_pms_updates'], config['Monitoring']['monitor_remote_access'],
                 config['Monitoring']['refresh_libraries_on_startup'], config['Monitoring']['refresh_libraries_interval']
                 )
            )
            server_id = c_db.lastrowid

            logger.debug(u"Multi-Server Migration - Modifying library_sections.server_id column.")
            c_db.execute(library_sections_temp)
            c_db.execute(
                'INSERT INTO library_sections_temp ('
                'section_id, section_name, section_type, agent, thumb, custom_thumb_url, '
                'art, count, parent_count, child_count, do_notify, do_notify_created, '
                'keep_history, deleted_section, server_id '
                ') SELECT '
                'section_id, section_name, section_type, agent, thumb, custom_thumb_url, '
                'art, count, parent_count, child_count, do_notify, do_notify_created, '
                'keep_history, deleted_section, %s AS server_id '
                'FROM library_sections WHERE server_id = "%s" ' % (server_id, config['PMS']['pms_identifier'])
            )
            c_db.execute(
                'DROP TABLE library_sections'
            )
            c_db.execute(
                'ALTER TABLE library_sections_temp RENAME TO library_sections'
            )

            logger.debug(u"Multi-Server Migration - Adding Server_ID to sessions table.")
            c_db.execute(
                'ALTER TABLE sessions ADD COLUMN server_id INTEGER'
            )
            c_db.execute(
                'UPDATE sessions '
                'SET server_id = %s' % server_id
            )

            logger.debug(u"Multi-Server Migration - Adding Server_ID to session_history table.")
            c_db.execute(
                'ALTER TABLE session_history ADD COLUMN server_id INTEGER'
            )
            c_db.execute(
                'UPDATE session_history '
                'SET server_id = %s' % server_id
            )

            logger.debug(u"Multi-Server Migration - Adding Server_ID to session_history_media_info table.")
            c_db.execute(
                'ALTER TABLE session_history_media_info ADD COLUMN server_id INTEGER '
            )
            c_db.execute(
                'UPDATE session_history_media_info '
                'SET server_id = %s' % server_id
            )

            logger.debug(u"Multi-Server Migration - Adding server_ID and library_ID to session_history_metadata table.")
            c_db.execute(
                'ALTER TABLE session_history_metadata ADD COLUMN server_id INTEGER '
            )
            c_db.execute(
                'ALTER TABLE session_history_metadata ADD COLUMN library_id INTEGER '
            )
            c_db.execute(
                'UPDATE session_history_metadata '
                '   SET server_id = %s '
                % server_id
            )
            c_db.execute(
                'UPDATE session_history_metadata '
                '   SET library_id = (SELECT id FROM library_sections WHERE session_history_metadata.server_id = library_sections.server_id AND session_history_metadata.section_id = library_sections.section_id)'
            )

            logger.debug(u"Multi-Server Migration - Creating user_shared_libraries table.")
            result = c_db.execute('SELECT id, user_token, server_token, shared_libraries from users ').fetchall()
            for row in result:
                key_dict = {'id': row[0],
                           'server_id': server_id
                           }
                value_dict = {}
                if row[1] != None: value_dict['user_token'] = row[1]
                if row[2] != None: value_dict['server_token'] = row[2]
                if row[3] != None and row[3] != '':
                    shared_libraries = []
                    for sl in tuple(row[3].split(';')):
                        lib_id = libraries.get_section_index(server_id=server_id, section_id=sl)
                        if lib_id and str(lib_id) not in shared_libraries:
                            shared_libraries.append(str(lib_id))
                    value_dict['shared_libraries'] = ";".join(shared_libraries)
                insert_query = (
                    "INSERT INTO user_shared_libraries (" + ", ".join(list(value_dict.keys()) + list(key_dict.keys())) + ")" +
                    " VALUES (" + ", ".join(["?"] * len(list(value_dict.keys()) + list(key_dict.keys()))) + ")"
                )
                c_db.execute(insert_query, list(value_dict.values()) + list(key_dict.values()))

            logger.debug(u"Multi-Server Migration - Migrating users table.")
            c_db.execute(
                'CREATE TABLE IF NOT EXISTS users_temp ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'user_id INTEGER DEFAULT NULL UNIQUE, username TEXT NOT NULL, friendly_name TEXT, '
                'thumb TEXT, custom_avatar_url TEXT, email TEXT, is_admin INTEGER DEFAULT 0, is_home_user INTEGER DEFAULT NULL, '
                'is_allow_sync INTEGER DEFAULT NULL, is_restricted INTEGER DEFAULT NULL, do_notify INTEGER DEFAULT 1, '
                'keep_history INTEGER DEFAULT 1, deleted_user INTEGER DEFAULT 0, allow_guest INTEGER DEFAULT 0, '
                'filter_all TEXT, filter_movies TEXT, filter_tv TEXT, filter_music TEXT, filter_photos TEXT '
                ') '
            )
            c_db.execute(
                'INSERT INTO users_temp ('
                'user_id, username, friendly_name, '
                'thumb, custom_avatar_url, email, is_admin, is_home_user, '
                'is_allow_sync, is_restricted, do_notify, '
                'keep_history, deleted_user, allow_guest, '
                'filter_all, filter_movies, filter_tv, filter_music, filter_photos'
                ') SELECT '
                'user_id, username, friendly_name, '
                'thumb, custom_avatar_url, email, is_admin, is_home_user, '
                'is_allow_sync, is_restricted, do_notify, '
                'keep_history, deleted_user, allow_guest, '
                'filter_all, filter_movies, filter_tv, filter_music, filter_photos '
                'FROM users'
            )
            c_db.execute(
                'DROP TABLE users'
            )
            c_db.execute(
                'ALTER TABLE users_temp RENAME TO users'
            )

            logger.debug(u"Multi-Server Migration - Migrating recently_added table.")
            c_db.execute(
                'CREATE TABLE IF NOT EXISTS recently_added_temp (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'added_at INTEGER, server_id INTEGER, section_id INTEGER, '
                'rating_key INTEGER, parent_rating_key INTEGER, grandparent_rating_key INTEGER, media_type TEXT, '
                'media_info TEXT)'
            )
            c_db.execute(
                'INSERT INTO recently_added_temp ('
                'added_at, server_id, section_id, rating_key, parent_rating_key, grandparent_rating_key, '
                'media_type, media_info'
                ') SELECT '
                'added_at, %s AS server_id, section_id, rating_key, '
                'parent_rating_key, grandparent_rating_key, '
                'media_type, media_info '
                'FROM recently_added '
                % (server_id)
            )
            c_db.execute(
                'DROP TABLE recently_added'
            )
            c_db.execute(
                'ALTER TABLE recently_added_temp RENAME TO recently_added'
            )

            logger.debug(u"Multi-Server Migration - Adding Server_ID to image_hash_lookup table.")
            c_db.execute(
                'ALTER TABLE image_hash_lookup ADD COLUMN server_id INTEGER'
            )
            c_db.execute(
                'UPDATE image_hash_lookup '
                'SET server_id = %s' % server_id
            )

            logger.debug(u"Multi-Server Migration - Adding server ID to themoviedb_lookup table.")
            c_db.execute(
                'ALTER TABLE themoviedb_lookup ADD COLUMN server_id INTEGER'
            )
            c_db.execute(
                'UPDATE themoviedb_lookup '
                'SET server_id = %s' % server_id
            )

            logger.debug(u"Multi-Server Migration - Adding server ID to the tvmaze_lookup table.")
            c_db.execute(
                'ALTER TABLE tvmaze_lookup ADD COLUMN server_id INTEGER'
            )
            c_db.execute(
                'UPDATE tvmaze_lookup '
                'SET server_id = %s' % server_id
            )

            logger.debug(u"Multi-Server Migration - Adding new notifiers to the notifiers table.")
            c_db.execute(
                'ALTER TABLE notifiers ADD COLUMN on_rclonedown INTEGER'
            )
            c_db.execute(
                'ALTER TABLE notifiers ADD COLUMN on_rclonedown_subject TEXT'
            )
            c_db.execute(
                'ALTER TABLE notifiers ADD COLUMN on_rclonedown_body TEXT'
            )
            c_db.execute(
                'ALTER TABLE notifiers ADD COLUMN on_rcloneup INTEGER'
            )
            c_db.execute(
                'ALTER TABLE notifiers ADD COLUMN on_rcloneup_subject TEXT'
            )
            c_db.execute(
                'ALTER TABLE notifiers ADD COLUMN on_rcloneup_body TEXT'
            )

            logger.debug(u"Multi-Server Migration - Adding server ID to the newsletter config table.")
            db = database.MonitorDatabase()
            result = db.select('SELECT id, newsletter_config from newsletters ')
            for row in result:
                newsletter_config = json.loads(row['newsletter_config'], '{}')
                if newsletter_config:
                    newsletter_config['incl_servers'] = [server_id]
                    incl_libraries = []
                    for section_id in newsletter_config['incl_libraries']:
                        lib_id = libraries.get_section_index(server_id, section_id)
                        if lib_id:
                            incl_libraries.append(lib_id)
                    newsletter_config['incl_libraries'] = incl_libraries
                    row['newsletter_config'] = json.dumps(newsletter_config)
                    row_keys = {'id': row.pop('id')}
                    db.upsert(table_name='newsletters', key_dict=row_keys, value_dict=row)

            logger.debug(u"Multi-Server Migration - Clearing Image Lookup Tables.")
            c_db.execute(
                'DELETE from image_hash_lookup'
            )
            c_db.execute(
                'DELETE from cloudinary_lookup'
            )
            c_db.execute(
                'DELETE from imgur_lookup'
            )

            logger.debug(u"Multi-Server Migration - Clearing Cache.")
            [os.remove(os.path.join(CONFIG.CACHE_DIR, f)) for f in os.listdir(CONFIG.CACHE_DIR)
             if f.endswith('.json')]
            for d in ['/images/', '/session_metadata/']:
                if os.path.exists(CONFIG.CACHE_DIR + d):
                    [os.remove(os.path.join(CONFIG.CACHE_DIR + d, f))
                        for f in os.listdir(CONFIG.CACHE_DIR + d)]


            logger.debug(u"Multi-Server Migration - Updating CONFIG Settings.")
            library_keys = []
            for section_id in CONFIG.HOME_LIBRARY_CARDS:
                result = c_db.execute(
                    'SELECT id FROM library_sections WHERE section_id = ?',
                    [int(section_id)]
                ).fetchone()
                if result:
                    library_keys.append(result[0])

            config['General']['home_library_cards'] = library_keys
            config['General']['home_sections'].insert(0, 'server_status')
            del config['Advanced']['jwt_secret']
            del config['PMS']['pms_name']
            del config['PMS']['pms_ip']
            del config['PMS']['pms_port']
            del config['PMS']['pms_is_remote']
            del config['PMS']['pms_ssl']
            del config['PMS']['pms_url']
            del config['PMS']['pms_identifier']
            del config['PMS']['pms_url_manual']
            del config['PMS']['pms_web_url']
            del config['PMS']['pms_use_bif']
            del config['PMS']['pms_update_channel']
            del config['PMS']['pms_url_override']
            del config['PMS']['pms_version']
            del config['PMS']['pms_update_distro']
            del config['PMS']['pms_update_distro_build']
            del config['PMS']['pms_platform']
            del config['PMS']['pms_is_cloud']
            del config['Monitoring']['monitor_pms_updates']
            del config['Monitoring']['monitor_remote_access']
            del config['Monitoring']['refresh_libraries_on_startup']
            del config['Monitoring']['refresh_libraries_interval']
            config.write()
            CONFIG.reload()

            logger.debug(u"Multi-Server Migration - Database Modifications completed successfully.")

    except sqlite3.OperationalError as e:
        logger.warn(u"Multi-Server Migration -  Database Modifications failed.")

    conn_db.commit()
    c_db.close()

    # Migrate poster_urls to imgur_lookup table
    try:
        db = database.MonitorDatabase()
        result = db.select('SELECT SQL FROM sqlite_master WHERE type="table" AND name="poster_urls"')
        if result:
            result = db.select('SELECT * FROM poster_urls')
            logger.debug(u"Altering database. Updating database table imgur_lookup.")

            data_factory = datafactory.DataFactory()

            for row in result:
                img_hash = notification_handler.set_hash_image_info(
                    rating_key=row['rating_key'], width=1000, height=1500, fallback='poster')
                data_factory.set_img_info(img_hash=img_hash, img_title=row['poster_title'],
                                          img_url=row['poster_url'], delete_hash=row['delete_hash'],
                                          service='imgur')

            db.action('DROP TABLE poster_urls')
    except sqlite3.OperationalError:
        pass


def upgrade():
    if CONFIG.UPDATE_NOTIFIERS_DB:
        notifiers.upgrade_config_to_db()
    if CONFIG.UPDATE_LIBRARIES_DB_NOTIFY:
        libraries.update_libraries_db_notify()


def shutdown(restart=False, update=False, checkout=False):
    global PMS_SERVERS
    logger.info(u"Stopping Tautulli web server...")
    cherrypy.engine.exit()

    # Shutdown the plexServers websocket connections
    PMS_SERVERS.stop()

    if SCHED.running:
        SCHED.shutdown(wait=False)
    if activity_handler.ACTIVITY_SCHED.running:
        activity_handler.ACTIVITY_SCHED.shutdown(wait=False)

    # Stop the notification threads
    for i in range(CONFIG.NOTIFICATION_THREADS):
        NOTIFY_QUEUE.put(None)

    CONFIG.write()

    if not restart and not update and not checkout:
        logger.info(u"Tautulli is shutting down...")

    if update:
        logger.info(u"Tautulli is updating...")
        try:
            versioncheck.update()
        except Exception as e:
            logger.warn(u"Tautulli failed to update: %s. Restarting." % e)

    if checkout:
        logger.info(u"Tautulli is switching the git branch...")
        try:
            versioncheck.checkout_git_branch()
        except Exception as e:
            logger.warn(u"Tautulli failed to switch git branch: %s. Restarting." % e)

    if CREATEPID:
        logger.info(u"Removing pidfile %s", PIDFILE)
        os.remove(PIDFILE)

    if WIN_SYS_TRAY_ICON:
        WIN_SYS_TRAY_ICON.shutdown()

    if restart:
        logger.info(u"Tautulli is restarting...")

        exe = sys.executable
        args = [exe, FULL_PATH]
        args += ARGS
        if '--nolaunch' not in args:
            args += ['--nolaunch']

        # Separate out logger so we can shutdown logger after
        if NOFORK:
            logger.info('Running as service, not forking. Exiting...')
        elif os.name == 'nt':
            logger.info('Restarting Tautulli with %s', args)
        else:
            logger.info('Restarting Tautulli with %s', args)

        logger.shutdown()

        # os.execv fails with spaced names on Windows
        # https://bugs.python.org/issue19066
        if NOFORK:
            pass
        elif os.name == 'nt':
            subprocess.Popen(args, cwd=os.getcwd())
        else:
            os.execv(exe, args)

    else:
        logger.shutdown()

    os._exit(0)


def generate_uuid():
    return uuid.uuid4().hex

#
# def initialize_tracker():
#     data = {
#         'dataSource': 'server',
#         'appName': common.PRODUCT,
#         'appVersion': common.RELEASE,
#         'appId': plexpy.INSTALL_TYPE,
#         'appInstallerId': plexpy.CONFIG.GIT_BRANCH,
#         'dimension1': '{} {}'.format(common.PLATFORM, common.PLATFORM_RELEASE),  # App Platform
#         'dimension2': common.PLATFORM_LINUX_DISTRO,  # Linux Distro
#         'userLanguage': plexpy.SYS_LANGUAGE,
#         'documentEncoding': plexpy.SYS_ENCODING,
#         'noninteractive': True
#         }
#
#     tracker = Tracker.create('UA-111522699-2', client_id=CONFIG.PMS_UUID, hash_client_id=True,
#                              user_agent=common.USER_AGENT)
#     tracker.set(data)
#
#     return tracker
#
#
# def analytics_event(category, action, label=None, value=None, **kwargs):
#     data = {'category': category, 'action': action}
#
#     if label is not None:
#         data['label'] = label
#
#     if value is not None:
#         data['value'] = value
#
#     if kwargs:
#         data.update(kwargs)
#
#     if TRACKER:
#         try:
#             TRACKER.send('event', data)
#         except Exception as e:
#             logger.warn(u"Failed to send analytics event for category '%s', action '%s': %s" % (category, action, e))


def check_folder_writable(folder, fallback, name):
    if not folder:
        folder = fallback

    if not os.path.exists(folder):
        try:
            os.makedirs(folder)
        except OSError as e:
            logger.error(u"Could not create %s dir '%s': %s" % (name, folder, e))
            if folder != fallback:
                logger.warn(u"Falling back to %s dir '%s'" % (name, fallback))
                return check_folder_writable(None, fallback, name)
            else:
                return folder, None

    if not os.access(folder, os.W_OK):
        logger.error(u"Cannot write to %s dir '%s'" % (name, folder))
        if folder != fallback:
            logger.warn(u"Falling back to %s dir '%s'" % (name, fallback))
            return check_folder_writable(None, fallback, name)
        else:
            return folder, False

    return folder, True
