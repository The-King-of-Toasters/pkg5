#!/usr/bin/python2.6
#
# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#
# Copyright (c) 2007, 2010 Oracle and/or its affiliates.  All rights reserved.
#

# pkg.depotd - package repository daemon

# XXX The prototype pkg.depotd combines both the version management server that
# answers to pkgsend(1) sessions and the HTTP file server that answers to the
# various GET operations that a pkg(1) client makes.  This split is expected to
# be made more explicit, by constraining the pkg(1) operations such that they
# can be served as a typical HTTP/HTTPS session.  Thus, pkg.depotd will reduce
# to a special purpose HTTP/HTTPS server explicitly for the version management
# operations, and must manipulate the various state files--catalogs, in
# particular--such that the pkg(1) pull client can operately accurately with
# only a basic HTTP/HTTPS server in place.

# XXX Although we pushed the evaluation of next-version, etc. to the pull
# client, we should probably provide a query API to do same on the server, for
# dumb clients (like a notification service).

# The default path for static and other web content.
CONTENT_PATH_DEFAULT = "/usr/share/lib/pkg"
# cherrypy has a max_request_body_size parameter that determines whether the
# server should abort requests with REQUEST_ENTITY_TOO_LARGE when the request
# body is larger than the specified size (in bytes).  The maximum size supported
# by cherrypy is 2048 * 1024 * 1024 - 1 (just short of 2048MB), but the default
# here is purposefully conservative.
MAX_REQUEST_BODY_SIZE = 128 * 1024 * 1024
# The default host/port(s) to serve data from.
HOST_DEFAULT = "0.0.0.0"
PORT_DEFAULT = 80
SSL_PORT_DEFAULT = 443
SOCKET_FILE_DEFAULT = ""
# The minimum number of threads allowed.
THREADS_MIN = 1
# The default number of threads to start.
THREADS_DEFAULT = 60
# The maximum number of threads that can be started.
THREADS_MAX = 5000
# The default server socket timeout in seconds. We want this to be longer than
# the normal default of 10 seconds to accommodate clients with poor quality
# connections.
SOCKET_TIMEOUT_DEFAULT = 60
# Whether modify operations should be allowed.
READONLY_DEFAULT = False
# Whether the repository catalog should be rebuilt on startup.
REBUILD_DEFAULT = False
# Whether the indexes should be rebuilt
REINDEX_DEFAULT = False
# Not in mirror mode by default
MIRROR_DEFAULT = False
# Not in link-local mirror mode my default
LL_MIRROR_DEFAULT = False

import getopt
import gettext
import locale
import logging
import os
import os.path
import OpenSSL.crypto as crypto
import subprocess
import sys
import tempfile
import urlparse

try:
        import cherrypy
        version = cherrypy.__version__.split('.')
        if map(int, version) < [3, 1, 0]:
                raise ImportError
        elif map(int, version) >= [3, 2, 0]:
                raise ImportError
except ImportError:
        print >> sys.stderr, """cherrypy 3.1.0 or greater (but less than """ \
            """3.2.0) is required to use this program."""
        sys.exit(2)

import cherrypy.process.servers

from pkg.misc import msg, emsg, setlocale
import pkg.client.api_errors as api_errors
import pkg.indexer as indexer
import pkg.portable.util as os_util
import pkg.search_errors as search_errors
import pkg.server.depot as ds
import pkg.server.depotresponse as dr
import pkg.server.repository as sr
import pkg.server.repositoryconfig as rc


class LogSink(object):
        """This is a dummy object that we can use to discard log entries
        without relying on non-portable interfaces such as /dev/null."""

        def write(self, *args, **kwargs):
                """Discard the bits."""
                pass

        def flush(self, *args, **kwargs):
                """Discard the bits."""
                pass


def usage(text=None, retcode=2, full=False):
        """Optionally emit a usage message and then exit using the specified
        exit code."""

        if text:
                emsg(text)

        if not full:
                # The full usage message isn't desired.
                emsg(_("Try `pkg.depotd --help or -?' for more "
                    "information."))
                sys.exit(retcode)

        print """\
Usage: /usr/lib/pkg.depotd [-d repo_dir] [-p port] [-s threads]
           [-t socket_timeout] [--cfg-file] [--content-root]
           [--disable-ops op[/1][,...]] [--debug feature_list]
           [--file-root dir] [--log-access dest] [--log-errors dest]
           [--mirror] [--nasty] [--set-property <section.property>=<value>]
           [--proxy-base url] [--readonly] [--rebuild] [--socket-path] 
           [--ssl-cert-file] [--ssl-dialog] [--ssl-key-file]
           [--sort-file-max-size size] [--writable-root dir]

        --add-content   Check the repository on startup and add any new
                        packages found.  Cannot be used with --mirror or 
                        --readonly.
        --cfg-file      The pathname of the file from which to read and to
                        write configuration information.
        --content-root  The file system path to the directory containing the
                        the static and other web content used by the depot's
                        browser user interface.  The default value is
                        '/usr/share/lib/pkg'.
        --disable-ops   A comma separated list of operations that the depot
                        should not configure.  If, for example, you wanted
                        to omit loading search v1, 'search/1' should be
                        provided as an argument, or to disable all search
                        operations, simply 'search'.
        --debug         The name of a debug feature to enable; or a whitespace
                        or comma separated list of features to enable.
                        Possible values are: headers.
        --exit-ready    Perform startup processing (including rebuilding
                        catalog or indices, if requested) and exit when
                        ready to start serving packages.
        --file-root     The path to the root of the file content for a given
                        repository.  This is used to override the default,
                        <repo_root>/file.
        --log-access    The destination for any access related information
                        logged by the depot process.  Possible values are:
                        stderr, stdout, none, or an absolute pathname.  The
                        default value is stdout if stdout is a tty; otherwise
                        the default value is none.
        --log-errors    The destination for any errors or other information
                        logged by the depot process.  Possible values are:
                        stderr, stdout, none, or an absolute pathname.  The
                        default value is stderr.
        --mirror        Package mirror mode; publishing and metadata operations
                        disallowed.  Cannot be used with --readonly or
                        --rebuild.
        --nasty         Instruct the server to misbehave.  At random intervals
                        it will time-out, send bad responses, hang up on
                        clients, and generally be hostile.  The option
                        takes a value (1 to 100) for how nasty the server
                        should be.
        --proxy-base    The url to use as the base for generating internal
                        redirects and content.
        --readonly      Read-only operation; modifying operations disallowed.
                        Cannot be used with --mirror or --rebuild.
        --rebuild       Re-build the catalog from pkgs in depot.  Cannot be
                        used with --mirror or --readonly.
        --set-property  Used to specify initial repository configuration
                        property values or to update existing ones; can
                        be specified multiple times.  If used with --readonly
                        this acts as a temporary override.
        --socket-path   The absolute pathname to a Unix domain socket.
                        If this option is specified, the depot will answer
                        connections through the UNIX socket instead of over IP.
        --ssl-cert-file The absolute pathname to a PEM-encoded Certificate file.
                        This option must be used with --ssl-key-file.  Usage of
                        this option will cause the depot to only respond to SSL
                        requests on the provided port.
        --ssl-dialog    Specifies what method should be used to obtain the
                        passphrase needed to decrypt the file specified by
                        --ssl-key-file.  Supported values are: builtin,
                        exec:/path/to/program, or smf:fmri.  The default value
                        is builtin.
        --ssl-key-file  The absolute pathname to a PEM-encoded Private Key file.
                        This option must be used with --ssl-cert-file.  Usage of
                        this option will cause the depot to only respond to SSL
                        requests on the provided port.
        --sort-file-max-size
                        The maximum size of the indexer sort file. Used to
                        limit the amount of RAM the depot uses for indexing,
                        or increase it for speed.
        --writable-root The path to a directory to which the program has write
                        access.  Used with --readonly to allow server to
                        create needed files, such as search indices, without
                        needing write access to the package information.
Options:
        --help or -?

Environment:
        PKG_REPO                Used as default repo_dir if -d not provided.
        PKG_DEPOT_CONTENT       Used as default content_root if --content-root
                                not provided."""
        sys.exit(retcode)

class OptionError(Exception):
        """Option exception. """

        def __init__(self, *args):
                Exception.__init__(self, *args)

if __name__ == "__main__":

        setlocale(locale.LC_ALL, "")
        gettext.install("pkg", "/usr/share/locale")

        debug_features = {
            "headers": False,
        }
        disable_ops = {}
        port = PORT_DEFAULT
        port_provided = False
        threads = THREADS_DEFAULT
        socket_timeout = SOCKET_TIMEOUT_DEFAULT
        readonly = READONLY_DEFAULT
        rebuild = REBUILD_DEFAULT
        reindex = REINDEX_DEFAULT
        proxy_base = None
        mirror = MIRROR_DEFAULT
        ll_mirror = LL_MIRROR_DEFAULT
        file_root = None
        nasty = False
        nasty_value = 0
        repo_config_file = None
        repo_path = None
        sort_file_max_size = indexer.SORT_FILE_MAX_SIZE
        socket_path = SOCKET_FILE_DEFAULT
        ssl_cert_file = None
        ssl_key_file = None
        ssl_dialog = "builtin"
        writable_root = None
        add_content = False
        exit_ready = False

        if "PKG_REPO" in os.environ:
                repo_path = os.environ["PKG_REPO"]

        try:
                content_root = os.environ["PKG_DEPOT_CONTENT"]
        except KeyError:
                try:
                        content_root = os.path.join(os.environ['PKG_HOME'],
                            'share/lib/pkg')
                except KeyError:
                        content_root = CONTENT_PATH_DEFAULT

        # By default, if the destination for a particular log type is not
        # specified, this is where we will send the output.
        log_routes = {
            "access": "none",
            "errors": "stderr"
        }
        log_opts = ["--log-%s" % log_type for log_type in log_routes]

        # If stdout is a tty, then send access output there by default instead
        # of discarding it.
        if os.isatty(sys.stdout.fileno()):
                log_routes["access"] = "stdout"

        opt = None
        repo_props = {}
        try:
                long_opts = ["add-content", "cfg-file=", "content-root=",
                    "debug=", "disable-ops=", "exit-ready", "file-root=",
                    "help", "llmirror", "mirror", "nasty=", "proxy-base=",
                    "readonly", "rebuild", "refresh-index", "set-property=",
                    "socket-path=", "ssl-cert-file=", "ssl-dialog=",
                    "ssl-key-file=", "sort-file-max-size=", "writable-root="]

                for opt in log_opts:
                        long_opts.append("%s=" % opt.lstrip('--'))
                opts, pargs = getopt.getopt(sys.argv[1:], "d:np:s:t:?",
                    long_opts)

                show_usage = False
                for opt, arg in opts:
                        if opt == "-n":
                                sys.exit(0)
                        elif opt == "-d":
                                repo_path = arg
                        elif opt == "-p":
                                port = int(arg)
                                port_provided = True
                        elif opt == "-s":
                                threads = int(arg)
                                if threads < THREADS_MIN:
                                        raise OptionError, \
                                            "minimum value is %d" % THREADS_MIN
                                if threads > THREADS_MAX:
                                        raise OptionError, \
                                            "maximum value is %d" % THREADS_MAX
                        elif opt == "-t":
                                socket_timeout = int(arg)
                        elif opt == "--add-content":
                                add_content = True
                        elif opt == "--cfg-file":
                                repo_config_file = os.path.abspath(arg)
                        elif opt == "--content-root":
                                if arg == "":
                                        raise OptionError, "You must specify " \
                                            "a directory path."
                                content_root = arg
                        elif opt == "--file-root":
                                if arg == "":
                                        raise OptionError, "You must specify " \
                                            "a directory path."
                                file_root = arg
                        elif opt == "--debug":
                                if arg is None or arg == "":
                                        raise OptionError, \
                                            "A debug feature must be specified."

                                # A list of features can be specified using a
                                # "," or any whitespace character as separators.
                                if "," in arg:
                                        features = arg.split(",")
                                else:
                                        features = arg.split()

                                for f in features:
                                        if f not in debug_features:
                                                raise OptionError, \
                                                    "Invalid debug feature: " \
                                                    "%s." % f
                                        debug_features[f] = True
                        elif opt == "--disable-ops":
                                if arg is None or arg == "":
                                        raise OptionError, \
                                            "An argument must be specified."

                                disableops = arg.split(",")
                                for s in disableops:
                                        if "/" in s:
                                                op, ver = s.rsplit("/", 1)
                                        else:
                                                op = s
                                                ver = "*"

                                        if op not in \
                                            ds.DepotHTTP.REPO_OPS_DEFAULT:
                                                raise OptionError(
                                                    "Invalid operation "
                                                    "'%s'." % s)

                                        disable_ops.setdefault(op, [])
                                        disable_ops[op].append(ver)
                        elif opt == "--exit-ready":
                                exit_ready = True
                        elif opt in log_opts:
                                if arg is None or arg == "":
                                        raise OptionError, \
                                            "You must specify a log " \
                                            "destination."
                                log_routes[opt.lstrip("--log-")] = arg
                        elif opt in ("--help", "-?"):
                                show_usage = True
                        elif opt == "--mirror":
                                mirror = True
                        elif opt == "--llmirror":
                                mirror = True
                                ll_mirror = True
                                readonly = True
                        elif opt == "--nasty":
                                value_err = None
                                try:
                                        nasty_value = int(arg)
                                except ValueError, e:
                                        value_err = e

                                if value_err or (nasty_value > 100 or
                                    nasty_value < 1):
                                        raise OptionError, "Invalid value " \
                                            "for nasty option.\n Please " \
                                            "choose a value between 1 and 100."
                                nasty = True
                        elif opt == "--set-property":
                                try:
                                        prop, p_value = arg.split("=", 1)
                                        p_sec, p_name = prop.split(".", 1)
                                except ValueError:
                                        usage(_("property arguments must be of "
                                            "the form '<section.property>="
                                            "<value>'."))
                                repo_props.setdefault(p_sec, {})
                                repo_props[p_sec][p_name] = p_value
                        elif opt == "--proxy-base":
                                # Attempt to decompose the url provided into
                                # its base parts.  This is done so we can
                                # remove any scheme information since we
                                # don't need it.
                                scheme, netloc, path, params, query, \
                                    fragment = urlparse.urlparse(arg,
                                    "http", allow_fragments=0)

                                if not netloc:
                                        raise OptionError, "Unable to " \
                                            "determine the hostname from " \
                                            "the provided URL; please use a " \
                                            "fully qualified URL."

                                scheme = scheme.lower()
                                if scheme not in ("http", "https"):
                                        raise OptionError, "Invalid URL; http " \
                                            "and https are the only supported " \
                                            "schemes."

                                # Rebuild the url with the sanitized components.
                                proxy_base = urlparse.urlunparse((scheme,
                                    netloc, path, params, query, fragment))
                        elif opt == "--readonly":
                                readonly = True
                        elif opt == "--rebuild":
                                rebuild = True
                        elif opt == "--refresh-index":
                                # Note: This argument is for internal use
                                # only. It's used when pkg.depotd is reexecing
                                # itself and needs to know that's the case.
                                # This flag is purposefully omitted in usage.
                                # The supported way to forcefully reindex is to
                                # kill any pkg.depot using that directory,
                                # remove the index directory, and restart the
                                # pkg.depot process. The index will be rebuilt
                                # automatically on startup.
                                reindex = True
                        elif opt == "--socket-path":
                                socket_path = arg
                        elif opt == "--ssl-cert-file":
                                if arg == "none":
                                        continue

                                ssl_cert_file = arg
                                if not os.path.isabs(ssl_cert_file):
                                        raise OptionError, "The path to " \
                                           "the Certificate file must be " \
                                           "absolute."
                                elif not os.path.exists(ssl_cert_file):
                                        raise OptionError, "The specified " \
                                            "file does not exist."
                                elif not os.path.isfile(ssl_cert_file):
                                        raise OptionError, "The specified " \
                                            "pathname is not a file."
                        elif opt == "--ssl-key-file":
                                if arg == "none":
                                        continue

                                ssl_key_file = arg
                                if not os.path.isabs(ssl_key_file):
                                        raise OptionError, "The path to " \
                                           "the Private Key file must be " \
                                           "absolute."
                                elif not os.path.exists(ssl_key_file):
                                        raise OptionError, "The specified " \
                                            "file does not exist."
                                elif not os.path.isfile(ssl_key_file):
                                        raise OptionError, "The specified " \
                                            "pathname is not a file."
                        elif opt == "--ssl-dialog":
                                if arg != "builtin" and not \
                                    arg.startswith("exec:/") and not \
                                    arg.startswith("smf:"):
                                        raise OptionError, "Invalid value " \
                                            "specified.  Expected: builtin, " \
                                            "exec:/path/to/program, or " \
                                            "smf:fmri."

                                f = arg
                                if f.startswith("exec:"):
                                        if os_util.get_canonical_os_type() != \
                                          "unix":
                                                # Don't allow a somewhat
                                                # insecure authentication method
                                                # on some platforms.
                                                raise OptionError, "exec is " \
                                                    "not a supported dialog " \
                                                    "type for this operating " \
                                                    "system."

                                        f = os.path.abspath(f.split(
                                            "exec:")[1])

                                        if not os.path.isfile(f):
                                                raise OptionError, "Invalid " \
                                                    "file path specified for " \
                                                    "exec."

                                        f = "exec:%s" % f

                                ssl_dialog = f
                        elif opt == "--sort-file-max-size":
                                if arg == "":
                                        raise OptionError, "You must specify " \
                                            "a maximum sort file size."
                                sort_file_max_size = arg
                        elif opt == "--writable-root":
                                if arg == "":
                                        raise OptionError, "You must specify " \
                                            "a directory path."
                                writable_root = arg
        except getopt.GetoptError, _e:
                usage("pkg.depotd: %s" % _e.msg)
        except OptionError, _e:
                usage("pkg.depotd: option: %s -- %s" % (opt, _e))
        except (ArithmeticError, ValueError):
                usage("pkg.depotd: illegal option value: %s specified " \
                    "for option: %s" % (arg, opt))

        if show_usage:
                usage(retcode=0, full=True)

        if rebuild and add_content:
                usage("--add-content cannot be used with --rebuild")
        if rebuild and reindex:
                usage("--refresh-index cannot be used with --rebuild")
        if (rebuild or add_content) and (readonly or mirror):
                usage("--readonly and --mirror cannot be used with --rebuild "
                    "or --add-content")
        if reindex and mirror:
                usage("--mirror cannot be used with --refresh-index")
        if reindex and readonly and not writable_root:
                usage("--readonly can only be used with --refresh-index if "
                    "--writable-root is used")

        if not repo_path and not file_root:
                usage("At least one of PKG_REPO, -d, or --file-root" 
                    " must be provided")

        if (ssl_cert_file and not ssl_key_file) or (ssl_key_file and not
            ssl_cert_file):
                usage("The --ssl-cert-file and --ssl-key-file options must "
                    "must both be provided when using either option.")
        elif ssl_cert_file and ssl_key_file and not port_provided:
                # If they didn't already specify a particular port, use the
                # default SSL port instead.
                port = SSL_PORT_DEFAULT

        # If the program is going to reindex, the port is irrelevant since
        # the program will not bind to a port.
        if not reindex and not exit_ready:
                try:
                        cherrypy.process.servers.check_port(HOST_DEFAULT, port)
                except Exception, e:
                        emsg("pkg.depotd: unable to bind to the specified "
                            "port: %d. Reason: %s" % (port, e))
                        sys.exit(1)
        else:
                # Not applicable if we're not going to serve content
                content_root = None

        key_data = None
        if not reindex and ssl_cert_file and ssl_key_file and \
            ssl_dialog != "builtin":
                cmdline = None
                def get_ssl_passphrase(*ignored):
                        p = None
                        try:
                                p = subprocess.Popen(cmdline, shell=True,
                                        stdout=subprocess.PIPE,
                                        stderr=None)
                                p.wait()
                        except Exception, __e:
                                emsg("pkg.depotd: an error occurred while "
                                    "executing [%s]; unable to obtain the "
                                    "passphrase needed to decrypt the SSL "
                                    "private key file: %s" % (cmdline, __e))
                                sys.exit(1)
                        return p.stdout.read().strip("\n")

                if ssl_dialog.startswith("exec:"):
                        cmdline = "%s %s %d" % (ssl_dialog.split("exec:")[1],
                            "''", port)
                elif ssl_dialog.startswith("smf:"):
                        cmdline = "/usr/bin/svcprop -p " \
                            "pkg_secure/ssl_key_passphrase %s" % (
                            ssl_dialog.split("smf:")[1])

                # The key file requires decryption, but the user has requested
                # exec-based authentication, so it will have to be decoded first
                # to an un-named temporary file.
                try:
                        key_file = file(ssl_key_file, "rb")
                        pkey = crypto.load_privatekey(crypto.FILETYPE_PEM,
                            key_file.read(), get_ssl_passphrase)

                        key_data = tempfile.TemporaryFile()
                        key_data.write(crypto.dump_privatekey(
                            crypto.FILETYPE_PEM, pkey))
                        key_data.seek(0)
                except EnvironmentError, _e:
                        emsg("pkg.depotd: unable to read the SSL private key "
                            "file: %s" % _e)
                        sys.exit(1)
                except crypto.Error, _e:
                        emsg("pkg.depotd: authentication or cryptography "
                            "failure while attempting to decode\nthe SSL "
                            "private key file: %s" % _e)
                        sys.exit(1)
                else:
                        # Redirect the server to the decrypted key file.
                        ssl_key_file = "/dev/fd/%d" % key_data.fileno()

        # Setup our global configuration.
        gconf = {
            "checker.on": True,
            "environment": "production",
            "log.screen": False,
            "server.max_request_body_size": MAX_REQUEST_BODY_SIZE,
            "server.shutdown_timeout": 0,
            "server.socket_file": socket_path,
            "server.socket_host": HOST_DEFAULT,
            "server.socket_port": port,
            "server.socket_timeout": socket_timeout,
            "server.ssl_certificate": ssl_cert_file,
            "server.ssl_private_key": ssl_key_file,
            "server.thread_pool": threads,
            "tools.log_headers.on": True,
            "tools.encode.on": True
        }

        if debug_features["headers"]:
                # Despite its name, this only logs headers when there is an
                # error; it's redundant with the debug feature enabled.
                gconf["tools.log_headers.on"] = False

                # Causes the headers of every request to be logged to the error
                # log; even if an exception occurs.
                gconf["tools.log_headers_always.on"] = True
                cherrypy.tools.log_headers_always = cherrypy.Tool(
                    "on_start_resource",
                    cherrypy.lib.cptools.log_request_headers)

        log_type_map = {
            "errors": {
                "param": "log.error_file",
                "attr": "error_log"
            },
            "access": {
                "param": "log.access_file",
                "attr": "access_log"
            }
        }

        for log_type in log_type_map:
                dest = log_routes[log_type]
                if dest in ("stdout", "stderr", "none"):
                        if dest == "none":
                                h = logging.StreamHandler(LogSink())
                        else:
                                h = logging.StreamHandler(eval("sys.%s" % \
                                    dest))

                        h.setLevel(logging.DEBUG)
                        h.setFormatter(cherrypy._cplogging.logfmt)
                        log_obj = eval("cherrypy.log.%s" % \
                            log_type_map[log_type]["attr"])
                        log_obj.addHandler(h)
                        # Since we've replaced cherrypy's log handler with our
                        # own, we don't want the output directed to a file.
                        dest = ""
                gconf[log_type_map[log_type]["param"]] = dest

        cherrypy.config.update(gconf)

        # Now that our logging, etc. has been setup, it's safe to perform any
        # remaining preparation.

        # Initialize repository state.
        fork_allowed = not reindex and not exit_ready  
        try:
                repo = sr.Repository(auto_create=not readonly,
                    cfgpathname=repo_config_file,  file_root=file_root,
                    fork_allowed=fork_allowed, log_obj=cherrypy,
                    mirror=mirror, properties=repo_props, read_only=readonly,
                    refresh_index=not add_content, repo_root=repo_path,
                    sort_file_max_size=sort_file_max_size,
                    writable_root=writable_root)
        except (RuntimeError, sr.RepositoryError), _e:
                emsg("pkg.depotd: %s" % _e)
                sys.exit(1)
        except rc.RequiredPropertyValueError, _e:
                emsg("pkg.depotd: repository configuration error: %s" % _e)
                emsg("Please use the --set-property option to provide a value, "
                    "or update the cfg_cache file for the repository to "
                    "correct this.")
                sys.exit(1)
        except rc.PropertyError, _e:
                emsg("pkg.depotd: repository configuration error: %s" % _e)
                sys.exit(1)
        except search_errors.IndexingException, _e:
                emsg("pkg.depotd: %s" % str(_e), "INDEX")
                sys.exit(1)
        except (api_errors.UnknownErrors, api_errors.PermissionsException), _e:
                emsg("pkg.depotd: %s" % str(_e))
                sys.exit(1)

        if reindex:
                # Initializing the repository above updated search indices
                # as needed; nothing left to do, so exit.
                sys.exit(0)

        if nasty:
                repo.cfg.set_nasty(nasty_value)

        if rebuild:
                try:
                        repo.rebuild()
                except sr.RepositoryError, e:
                        emsg(str(e), "REBUILD")
                        sys.exit(1)
                except (search_errors.IndexingException,
                    api_errors.UnknownErrors,
                    api_errors.PermissionsException), e:
                        emsg(str(e), "INDEX")
                        sys.exit(1)

        elif add_content:
                try:
                        repo.add_content()
                except sr.RepositoryError, e:
                        emsg(str(e), "ADD_CONTENT")
                        sys.exit(1)
                except (search_errors.IndexingException,
                    api_errors.UnknownErrors,
                    api_errors.PermissionsException), e:
                        emsg(str(e), "INDEX")
                        sys.exit(1)

        # ready to start depot; exit now if requested
        if exit_ready:
                sys.exit(0)

        # Next, initialize depot.
        if nasty:
                depot = ds.NastyDepotHTTP(repo, content_root,
                    disable_ops=disable_ops)
        else:
                depot = ds.DepotHTTP(repo, content_root,
                    disable_ops=disable_ops)

        # Now build our site configuration.
        conf = {
            "/": {
                # We have to override cherrypy's default response_class so that
                # we have access to the write() callable to stream data
                # directly to the client.
                "wsgi.response_class": dr.DepotResponse,
            },
            "/robots.txt": {
                "tools.staticfile.on": True,
                "tools.staticfile.filename": os.path.join(depot.web_root,
                    "robots.txt")
            },
        }

        if proxy_base:
                # This changes the base URL for our server, and is primarily
                # intended to allow our depot process to operate behind Apache
                # or some other webserver process.
                #
                # Visit the following URL for more information:
                #    http://cherrypy.org/wiki/BuiltinTools#tools.proxy
                proxy_conf = {
                        "tools.proxy.on": True,
                        "tools.proxy.local": "",
                        "tools.proxy.base": proxy_base
                }

                # Now merge or add our proxy configuration information into the
                # existing configuration.
                for entry in proxy_conf:
                        conf["/"][entry] = proxy_conf[entry]

        if ll_mirror:
                ds.DNSSD_Plugin(cherrypy.engine, conf, gconf).subscribe()

        try:
                root = cherrypy.Application(depot)
                cherrypy.quickstart(root, config=conf)
        except Exception, _e:
                emsg("pkg.depotd: unknown error starting depot server, " \
                    "illegal option value specified?")
                emsg(_e)
                sys.exit(1)
