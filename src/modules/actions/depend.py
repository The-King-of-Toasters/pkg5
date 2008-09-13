#!/usr/bin/python2.4
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

#
# Copyright 2008 Sun Microsystems, Inc.  All rights reserved.
# Use is subject to license terms.
#

"""Action describing a package dependency.

This module contains the DependencyAction class, which represents a
relationship between the package containing the action and another package.
"""

import urllib
import generic
import pkg.fmri as fmri

# for fmri correction hack
import re

class DependencyAction(generic.Action):
        """Class representing a dependency packaging object.  The fmri attribute
        is expected to be the pkg FMRI that this package depends on.  The type
        attribute is one of

        optional - dependency if present activates additional functionality,
                   but is not needed

        require - dependency is needed for correct function

        transfer - dependency on minimum version of other package that donated
        components to this package at earlier version.  Other package need not
        be installed, but if it is, it must be at the specified version.  Effect
        is the same as optional, but semantics are different.

        incorporate - optional freeze at specified version

        exclude - package non-functional if dependent package is present
        (unimplemented) """

        name = "depend"
        attributes = ("type", "fmri")
        key_attr = "fmri"
        known_types = ("optional", "require", "transfer", "incorporate")

        def __init__(self, data=None, **attrs):
                generic.Action.__init__(self, data, **attrs)
                if "fmri" in self.attrs:
                        self.clean_fmri()

        def clean_fmri(self):
                """ Clean up an invalid depend fmri into one which
                we can recognize.
                Example: 2.01.01.38-0.96  -> 2.1.1.38-0.96
                This also corrects self.attrs["fmri"] as external code
                knows about that, too.
                """
                #
                # This hack corrects a problem in pre-2008.11 packaging
                # metadata: some depend actions were specified with invalid
                # fmris of the form 2.38.01.01.3 (the padding zero is considered
                # invalid).  When we get an invalid FMRI, we use regular
                # expressions to perform a replacement operation which
                # cleans up these problems.
                #
                # n.b. that this parser is not perfect: it will fix only
                # the 'release' part of depend fmris.
                #
                fmri_string = self.attrs["fmri"]

                #
                # Start by locating the @ and the "," or "-" or ":" which
                # is to the right of said @.
                #
                verbegin = fmri_string.find("@")
                if verbegin == -1:
                        return
                verend = fmri_string.find(",", verbegin)
                if verend == -1:
                        verend = fmri_string.find("-", verbegin)
                if verend == -1:
                        verend = fmri_string.find(":", verbegin)
                if verend == -1:
                        verend = len(fmri_string)
                # skip over the @ sign
                verbegin += 1
                verdots = fmri_string[verbegin:verend]
                dots = verdots.split(".")
                if len(dots) == 0:
                        return
                # Do the correction
                cleanvers = ".".join([str(int(x)) for x in dots])
                cleanfmri = fmri_string[:verbegin] + \
                    cleanvers + fmri_string[verend:]
                # XXX enable if you need to debug
                #if cleanfmri != fmri_string:
                #       print "corrected invalid fmri: %s -> %s" % \
                #           (fmri_string, cleanfmri)
                self.attrs["fmri"] = cleanfmri


        def parse(self, image):
                """ decodes attributes into tuple whose contents are
                (boolean required, minimum fmri, maximum fmri)
                XXX still needs exclude support....
                """
                type = self.attrs["type"]

                pkgfmri = self.attrs["fmri"]
                f = fmri.PkgFmri(pkgfmri, image.attrs["Build-Release"])
                image.fmri_set_default_authority(f)

                min_fmri = f
                max_fmri = None
                required = True
                if type == "optional" or type == "transfer":
                        required = False
                elif type == "incorporate":
                        required = False
                        max_fmri = f
                return required, min_fmri, max_fmri


        def verify(self, image, **args):
                # XXX maybe too loose w/ early versions

                type = self.attrs["type"]

                if type not in self.known_types:
                        return ["Unknown type (%s) in depend action" % type]

                pkgfmri = self.attrs["fmri"]
                f = fmri.PkgFmri(pkgfmri, image.attrs["Build-Release"])
                installed_version = image.has_version_installed(f)

                if not installed_version:
                        if type == "require":
                                return ["Required dependency %s is not installed" % fm]
                        installed_version = image.older_version_installed(f)
                        if installed_version:
                                return ["%s dependency %s is downrev (%s)" %
                                        (type, f, installed_version)]

                #XXX - leave off for now since we can't handle max
                # fmri constraint w/o backtracking
                #elif type == "incorporate":
                #        if not image.is_installed(f):
                #                return ["%s dependency %s is uprev (%s)" %
                #                        (type, f, installed_version)]
                return []

        def generate_indices(self):
                type = self.attrs["type"]
                fmri = self.attrs["fmri"]

                if type not in self.known_types:
                        return {}

                #
                # XXX Ideally, we'd turn the string into a PkgFmri, and separate
                # the stem from the version, or use get_dir_path, but we can't
                # create a PkgFmri without supplying a build release and without
                # it creating a dummy timestamp.  So we have to split it apart
                # manually.
                #
                # XXX This code will need to change once we start using fmris
                # with authorities.
                #
                if fmri.startswith("pkg:/"):
                        fmri = fmri[5:]
                # Note that this creates a directory hierarchy!
                fmri = urllib.quote(fmri, "@").replace("@", "/")

                return {
                    "depend": fmri
                }
