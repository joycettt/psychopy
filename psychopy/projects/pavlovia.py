#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Part of the PsychoPy library
# Copyright (C) 2018 Jonathan Peirce
# Distributed under the terms of the GNU General Public License (GPL).

"""Helper functions in PsychoPy for interacting with Pavlovia.org
"""
from future.builtins import object
import glob
import os, sys, time
from psychopy import logging, prefs, constants
from psychopy.tools.filetools import DictStorage
from psychopy import app
from psychopy.localization import _translate
import gitlab
import gitlab.v4.objects
import git
import subprocess
import requests
import traceback
# for authentication
from uuid import uuid4
from .gitignore import gitIgnoreText

# TODO: test what happens if we have a network initially but lose it
# TODO: test what happens if we have a network but pavlovia times out

pavloviaPrefsDir = os.path.join(prefs.paths['userPrefsDir'], 'pavlovia')
rootURL = "https://gitlab.pavlovia.org"
client_id = '4bb79f0356a566cd7b49e3130c714d9140f1d3de4ff27c7583fb34fbfac604e0'
scopes = []
redirect_url = 'https://gitlab.pavlovia.org/'

knownUsers = DictStorage(
    filename=os.path.join(pavloviaPrefsDir, 'users.json'))

# knownProjects is a dict stored by id ("namespace/name")
knownProjects = DictStorage(
    filename=os.path.join(pavloviaPrefsDir, 'projects.json'))
# knownProjects stores the gitlab id to check if it's the same exact project
# We add to the knownProjects when project.local is set (ie when we have a
# known local location for the project)

permissions = {  # for ref see https://docs.gitlab.com/ee/user/permissions.html
    'guest': 10,
    'reporter': 20,
    'developer': 30,  # (can push to non-protected branches)
    'maintainer': 30,
    'owner': 50}


def getAuthURL():
    state = str(uuid4())  # create a private "state" based on uuid
    auth_url = ('https://gitlab.pavlovia.org/oauth/authorize?client_id={}'
                '&redirect_uri={}&response_type=token&state={}'
                .format(client_id, redirect_url, state))
    return auth_url, state


def login(tokenOrUsername,  rememberMe=True):
    """Sets the current user by means of a token

    Parameters
    ----------
    token
    """
    currentSession = getCurrentSession()
    if not currentSession:
        raise ConnectionError("Failed to connect to Pavlovia.org. No network?")
    # would be nice here to test whether this is a token or username
    logging.debug('pavloviaTokensCurrently: {}'.format(knownUsers))
    if tokenOrUsername in knownUsers:
        token = knownUsers[tokenOrUsername]  # username so fetch token
    else:
        token = tokenOrUsername

    # try actually logging in with token
    currentSession.setToken(token)
    user = User(gitlabData=currentSession.user, rememberMe=rememberMe)
    prefs.appData['projects']['pavloviaUser'] = user.username


def logout():
    """Log the current user out of pavlovia.

    NB This function does not delete the cookie from the wx mini-browser
    if that has been set. Use pavlovia_ui for that.

     - set the user for the currentSession to None
     - save the appData so that the user is blank
    """
    # create a new currentSession with no auth token
    global _existingSession
    _existingSession = PavloviaSession()
    _existingSession.user = None
    # set appData to None
    prefs.appData['projects']['pavloviaUser'] = None
    prefs.saveAppData()
    for frameWeakref in app.openFrames:
        frame = frameWeakref()
        if hasattr(frame, 'setUser'):
            frame.setUser(None)


class User(object):
    """Class to combine what we know about the user locally and on gitlab

    (from previous logins and from the current session)"""
    def __init__(self, localData={}, gitlabData=None, rememberMe=True):
        currentSession = getCurrentSession()
        self.data = localData
        self.gitlabData = gitlabData
        # try looking for local data
        if gitlabData and not localData:
            if gitlabData.username in knownUsers:
                self.data = knownUsers[gitlabData.username]
        #then try again to populate fields
        if gitlabData and not localData:
            self.data['username'] = gitlabData.username
            self.data['token'] = currentSession.getToken()
            self.avatar = gitlabData.attributes['avatar_url']
        elif 'avatar' in localData:
            self.avatar = localData['avatar']
        elif gitlabData:
            self.avatar = gitlabData.attributes['avatar_url']
        if rememberMe:
            self.saveLocal()

    def __str__(self):
        return "pavlovia.User <{}>".format(self.username)

    def __getattr__(self, name):
        if name not in self.__dict__ and hasattr(self.gitlabData, name):
            return getattr(self.gitlabData, name)
        raise AttributeError("No attribute '{}' in this PavloviaUser".format(name))

    @property
    def username(self):
        if 'username' in self.gitlabData.attributes:
            return self.gitlabData.username
        elif 'username' in self.data:
            return self.data['username']
        else:
            return None

    @property
    def url(self):
        return self.gitlabData.web_url

    @property
    def name(self):
        return self.gitlabData.name

    @name.setter
    def name(self, name):
        self.gitlabData.name = name

    @property
    def token(self):
        return self.data['token']

    @property
    def avatar(self):
        if 'avatar' in self.data:
            return self.data['avatar']
        else:
            return None

    @avatar.setter
    def avatar(self, location):
        if os.path.isfile(location):
            self.data['avatar'] = location

    def _fetchRemoteAvatar(self, url=None):
        if not url:
            url = self.avatar_url
        exten = url.split(".")[-1]
        if exten not in ['jpg', 'png', 'tif']:
            exten = 'jpg'
        avatarLocal = os.path.join(pavloviaPrefsDir, ("avatar_{}.{}"
                                         .format(self.username, exten)))

        # try to fetch the actual image file
        r = requests.get(url, stream=True)
        if r.status_code == 200:
            with open(avatarLocal, 'wb') as f:
                for chunk in r:
                    f.write(chunk)
            return avatarLocal
        return None

    def saveLocal(self):
        """Saves the data on the current user in the pavlovia/users json file"""
        # update stored tokens
        tokens = knownUsers
        tokens[self.username] = self.data
        tokens.save()

    def save(self):
        self.gitlabData.save()


class PavloviaSession:
    """A class to track a session with the server.

    The session will store a token, which can then be used to authenticate
    for project read/write access
    """

    def __init__(self, token=None, remember_me=True):
        """Create a session to send requests with the pavlovia server

        Provide either username and password for authentication with a new
        token, or provide a token from a previous session, or nothing for an
        anonymous user
        """
        self.username = None
        self.password = None
        self.userID = None  # populate when token property is set
        self.userFullName = None
        self.remember_me = remember_me
        self.authenticated = False
        self.currentProject = None
        self.setToken(token)

    def createProject(self, name, description="", tags=(), visibility='private',
                      localRoot='', namespace=''):
        """Returns a PavloviaProject object (derived from a gitlab.project)

        Parameters
        ----------
        name
        description
        tags
        visibility
        local

        Returns
        -------
        a PavloviaProject object

        """
        if not self.user:
            raise NoUserError("Tried to create project with no user logged in")
        # NB gitlab also supports "internal" (public to registered users)
        if type(visibility) == bool and visibility:
            visibility = 'public'
        elif type(visibility) == bool and not visibility:
            visibility = 'private'

        projDict = {}
        projDict['name'] = name
        projDict['description'] = description
        projDict['issues_enabled'] = True
        projDict['visibility'] = visibility
        projDict['wiki_enabled'] = True
        if namespace and namespace != self.username:
            namespaceRaw = self.getNamespace(namespace)
            if namespaceRaw:
                projDict['namespace_id'] = namespaceRaw.id
            else:
                raise ValueError("PavloviaSession.createProject was given a "
                                 "namespace that couldn't be found on gitlab.")
        # TODO: add avatar option?
        # TODO: add namespace option?
        gitlabProj = self.gitlab.projects.create(projDict)
        pavProject = PavloviaProject(gitlabProj, localRoot=localRoot)
        return pavProject

    def getProject(self, id):
        """Gets a Pavlovia project from an ID number or namespace/name

        Parameters
        ----------
        id a numerical

        Returns
        -------
        pavlovia.PavloviaProject or None

        """
        if id:
            return PavloviaProject(id)
        else:
            return None

    def findProjects(self, search_str='', tags="psychopy"):
        """
        Parameters
        ----------
        search_str : str
            The string to search for in the title of the project
        tags : str
            Comma-separated string containing tags

        Returns
        -------
        A list of OSFProject objects

        """
        rawProjs = self.gitlab.projects.list(
            search=search_str,
            as_list=False)  # iterator not list for auto-pagination
        projs = [PavloviaProject(proj) for proj in rawProjs if proj.id]
        return projs

    def listUserGroups(self, namesOnly=True):
        gps = self.gitlab.groups.list(member=True)
        if namesOnly:
            gps = [this.name for this in gps]
        return gps

    def findUserProjects(self, searchStr=''):
        """Finds all readable projects of a given user_id
        (None for current user)
        """
        own = self.gitlab.projects.list(owned=True, search=searchStr)
        group = self.gitlab.projects.list(owned=False, membership=True,
                                          search = searchStr)
        projs = []
        projIDs = []
        for proj in own + group:
            if proj.id and proj.id not in projIDs:
                projs.append(PavloviaProject(proj))
                projIDs.append(proj.id)
        return projs

    def findUsers(self, search_str):
        """Find user IDs whose name matches a given search string
        """
        return self.gitlab.users

    def getToken(self):
        """The authorisation token for the current logged in user
        """
        return self.__dict__['token']

    def setToken(self, token):
        """Set the token for this session and check that it works for auth
        """
        self.__dict__['token'] = token
        self.startSession(token)

    def getNamespace(self, namespace):
        """Returns a namespace object for the given name if an exact match is
        found
        """
        spaces = self.gitlab.namespaces.list(search=namespace)
        # might be more than one, with
        for thisSpace in spaces:
            if thisSpace.path == namespace:
                return thisSpace

    def startSession(self, token):
        """Start a gitlab session as best we can
        (if no token then start an empty session)"""
        if token:
            if len(token) < 64:
                raise ValueError(
                    "Trying to login with token {} which is shorter "
                    "than expected length ({} not 64) for gitlab token"
                    .format(repr(token), len(token)))
            self.gitlab = gitlab.Gitlab(rootURL, oauth_token=token, timeout=2)
            self.gitlab.auth()
        else:
            self.gitlab = gitlab.Gitlab(rootURL, timeout=1)

    def applyChanges(self):
        """If threaded up/downloading is enabled then this begins the process
        """
        raise NotImplemented

    @property
    def user(self):
        if hasattr(self.gitlab, 'user'):
            return self.gitlab.user
        else:
            return None


class PavloviaProject(dict):
    """A Pavlovia project, with name, url etc

    .pavlovia will point to a gitlab project on gitlab.pavlovia.org
    .repo will will be a gitpython repo
    .id is the namespace/name (e.g. peircej/stroop)
    .idNumber is gitlab numeric id
    .title
    .tags
    .owner is technically the namespace. Get the owner from .attributes['owner']
    .localRoot is the path to the local root
    """

    def __init__(self, proj, localRoot=''):
        dict.__init__(self)
        self._storedAttribs = {}  # these will go into knownProjects file
        self['id'] = ''
        self['localRoot'] = ''
        self['remoteSSH'] = ''
        self['remoteHTTPS'] = ''
        self._lastKnownSync = 0
        currentSession = getCurrentSession()
        self._newRemote = False  # False can also indicate 'unknown'
        if isinstance(proj, gitlab.v4.objects.Project):
            self.pavlovia = proj
        elif currentSession.gitlab is None:
            self.pavlovia = None
        else:
            self.pavlovia = currentSession.gitlab.projects.get(proj)

        self.repo = None  # update with getRepo()
        # do we already have a local folder for this?
        if self.id in knownProjects and not localRoot:
            self.localRoot = knownProjects[self.id]['localRoot']
        else:
            self.localRoot = localRoot

    def __getattr__(self, name):
        if name == 'owner':
            return
        proj = self.__dict__['pavlovia']
        toSearch = [self, self.__dict__, proj._attrs]
        if 'attributes' in self.pavlovia.__dict__:
            toSearch.append(self.pavlovia.__dict__['attributes'])
        for attDict in toSearch:
            if name in attDict:
                return attDict[name]
        # error if none found
        if name == 'id':
            selfDescr = "PavloviaProject"
        else:
            selfDescr = repr(
                self)  # this includes self.id so don't use if id fails!
        raise AttributeError("No attribute '{}' in {}".format(name, selfDescr))

    @property
    def pavlovia(self):
        return self.__dict__['pavlovia']

    @pavlovia.setter
    def pavlovia(self, proj):
        global knownProjects
        self.__dict__['pavlovia'] = proj
        thisID = proj.attributes['path_with_namespace']
        if thisID in knownProjects \
                and os.path.exists(knownProjects[thisID]['localRoot']):
            rememberedProj = knownProjects[thisID]
            if rememberedProj['idNumber'] != proj.attributes['id']:
                logging.warning("Project {} has changed gitlab ID since last "
                                "use (was {} now {})"
                                .format(thisID,
                                        rememberedProj['idNumber'],
                                        proj.attributes['id']))
            self.update(rememberedProj)
        else:
            self['localRoot'] = ''
            self['id'] = proj.attributes['path_with_namespace']
            self['idNumber'] = proj.attributes['id']
        self['remoteSSH'] = proj.ssh_url_to_repo
        self['remoteHTTPS'] = proj.http_url_to_repo

    @property
    def emptyRemote(self):
        return not bool(self.pavlovia.attributes['default_branch'])

    @property
    def localRoot(self):
        return self['localRoot']

    @localRoot.setter
    def localRoot(self, localRoot):
        self['localRoot'] = localRoot
        # this is where we add a project to knownProjects:
        if localRoot:  # i.e. not set to None or ''
            knownProjects[self.id] = self

    @property
    def id(self):
        if 'id' in self.pavlovia.attributes:
            return self.pavlovia.attributes['path_with_namespace']

    @property
    def idNumber(self):
        return self.pavlovia.attributes['id']

    @property
    def owner(self):
        return self.pavlovia.attributes['namespace']['name']

    @property
    def attributes(self):
        return self.pavlovia.attributes

    @property
    def title(self):
        """The title of this project (alias for name)
        """
        return self.name

    @property
    def tags(self):
        """The title of this project (alias for name)
        """
        return self.tag_list

    def sync(self, syncPanel=None, progressHandler=None):
        """Performs a pull-and-push operation on the remote

        Will check for a local folder and whether that is already (in) a repo.
        If we have a local folder and it is not a git project already then
        this function will also clone the remote to that local folder

        Optional params syncPanel and progressHandler are both needed if you
        want to update a sync window/panel
        """
        if not self.repo:  # if we haven't been given a local copy of repo then find
            self.getRepo(progressHandler=progressHandler)
            # if cloned in last 2s then it was a fresh clone
            if time.time() < self._lastKnownSync + 2:
                return 1
        # pull first then push
        t0 = time.time()
        if self.emptyRemote:  # we don't have a repo there yet to do a 1st push
            self.firstPush()
        else:
            self.pull(syncPanel=syncPanel, progressHandler=progressHandler)
            self.repo = git.Repo(self.localRoot)  # get a new copy of repo (
            time.sleep(0.1)
            self.push(syncPanel=syncPanel, progressHandler=progressHandler)
        self._lastKnownSync = t1 = time.time()
        msg = ("Successful sync at: {}, took {:.3f}s"
               .format(time.strftime("%H:%M:%S", time.localtime()), t1 - t0))
        logging.info(msg)
        if syncPanel:
            syncPanel.statusAppend("\n"+msg)
            time.sleep(0.5)

    def pull(self, syncPanel=None, progressHandler=None):
        """Pull from remote to local copy of the repository

        Parameters
        ----------
        syncPanel
        progressHandler

        Returns
        -------

        """
        if syncPanel:
            syncPanel.statusAppend("\nPulling changes from remote...")
        origin = self.repo.remotes.origin
        info = self.repo.git.pull()  # progress=progressHandler
        logging.debug('pull report: {}'.format(info))
        if syncPanel:
            syncPanel.statusAppend("done")
            if info:
                syncPanel.statusAppend("\n{}".format(info))


    def push(self, syncPanel=None, progressHandler=None):
        """Push to remote from local copy of the repository

        Parameters
        ----------
        syncPanel
        progressHandler

        Returns
        -------

        """
        if syncPanel:
            syncPanel.statusAppend("\nPushing changes to remote...")
        origin = self.repo.remotes.origin
        info = self.repo.git.push()  # progress=progressHandler
        logging.debug('push report: {}'.format(info))
        if syncPanel:
            syncPanel.statusAppend("done")
            if info:
                syncPanel.statusAppend("\n{}".format(info))

    def getRepo(self, syncPanel=None, progressHandler=None, forceRefresh=False,
                newRemote=False):
        """Will always try to return a valid local git repo

        Will try to clone if local is empty and remote is not"""
        if self.repo and not forceRefresh:
            return self.repo
        if not self.localRoot:
            raise AttributeError("Cannot fetch a PavloviaProject until we have "
                                 "chosen a local folder.")
        gitRoot = getGitRoot(self.localRoot)
        if gitRoot is None:
            self.newRepo(progressHandler)
        elif gitRoot != self.localRoot:
            # this indicates that the requested root is inside another repo
            raise AttributeError("The requested local path for project\n\t{}\n"
                                 "sits inside another folder, which git will "
                                 "not permit. You might like to set the "
                                 "project local folder to be \n\t{}"
                                 .format(repr(self.localRoot), repr(gitRoot)))
        else:
            repo = git.Repo(gitRoot)
        self.repo = repo
        self.writeGitIgnore()

    def writeGitIgnore(self):
        """Check that a .gitignore file exists and add it if not"""
        gitIgnorePath = os.path.join(self.localRoot, '.gitignore')
        if not os.path.exists(gitIgnorePath):
            with open(gitIgnorePath, 'w') as f:
                f.write(gitIgnoreText)

    def newRepo(self, progressHandler=None):
        """Will either git.init and git.push or git.clone depending on state
        of local files.

        Use newRemote if we know that the remote has only just been created
        and is empty
        """
        localFiles = glob.glob(os.path.join(self.localRoot, "*"))
        # there's no project at all so create one
        if not self.localRoot:
            raise AttributeError("Cannot fetch a PavloviaProject until we have "
                                 "chosen a local folder.")
        if localFiles and self._newRemote:  # existing folder
            self.repo = git.Repo.init(self.localRoot)
            with self.repo.config_writer() as config:
                config.set_value("user", "email", self.pavlovia.user.email)
                config.set_value("user", "name", self.pavlovia.user.name)
            # add origin remote and master branch (but no push)
            self.repo.create_remote('origin', url=self['remoteHTTPS'])
            self.repo.git.checkout(b="master")
            self.writeGitIgnore()
            self.stageFiles(['.gitignore'])
            self.commit(['Create repository (including .gitignore)'])
            self._newRemote = True
        else:
            # no files locally so safe to try and clone from remote
            self.cloneRepo(progressHandler)
            # TODO: add the further case where there are remote AND local files!

    def firstPush(self):
        self.repo.git.push('-u', 'origin', 'master')

    def cloneRepo(self, progressHandler=None):
        """Gets the git.Repo object for this project, creating one if needed

        Will check for a local folder and whether that is already (in) a repo.
        If we have a local folder and it is not a git project already then
        this function will also clone the remote to that local folder

        Parameters
        ----------
        progressHandler is subclassed from gitlab.remote.RemoteProgress

        Returns
        -------
        git.Repo object

        Raises
        ------
        AttributeError if the local project is inside a git repo

        """
        if not self.localRoot:
            raise AttributeError("Cannot fetch a PavloviaProject until we have "
                                 "chosen a local folder.")
        if progressHandler:
            progressHandler.setStatus("Cloning from remote...")
            progressHandler.syncPanel.Refresh()
            progressHandler.syncPanel.Layout()
        repo = git.Repo.clone_from(
            self.remoteHTTPS,
            self.localRoot,
            # progress=progressHandler,
        )
        self._lastKnownSync = time.time()
        self.repo = repo
        self._newRemote = False

    def forkTo(self, username=None):
        if username:
            # fork to a specific namespace
            fork = self.pavlovia.forks.create({'namespace': 'myteam'})
        else:
            fork = self.pavlovia.forks.create(
                {})  # uses the current logged-in user
        return fork

    def getChanges(self):
        """Find all the not-yet-committed changes in the repository"""
        changeDict = {}
        changeDict['untracked'] = self.repo.untracked_files
        changeDict['changed'] = []
        changeDict['deleted'] = []
        changeDict['renamed'] = []
        for this in self.repo.index.diff(None):
            # change type, identifying possible ways a blob can have changed
            # A = Added
            # D = Deleted
            # R = Renamed
            # M = Modified
            # T = Changed in the type
            if this.change_type == 'D':
                changeDict['deleted'].append(this.b_path)
            elif this.change_type == 'R':  # only if git rename had been called?
                changeDict['renamed'].append((this.rename_from, this.rename_to))
            elif this.change_type == 'M':
                changeDict['changed'].append(this.b_path)
            else:
                raise (
                    "Found an unexpected change_type '{}' in gitpython Diff"
                        .format(this.change_type))
        changeList = []
        for categ in changeDict:
            changeList.extend(changeDict[categ])
        return changeDict, changeList

    def stageFiles(self, files=None):
        """Adds changed files to the stage (index) ready for commit.

        The files is a list and can include new/changed/deleted

        If files=None this is like `git add -u` (all files added/deleted)
        """
        if files:
            if type(files) not in (list, tuple):
                raise TypeError(
                    'The `files` provided to PavloviaProject.stageFiles '
                    'should be a list not a {}'.format(type(files)))
            self.repo.git.add(files)
        else:
            diffsDict, diffsList = self.getChanges()
            if diffsDict['untracked']:
                self.repo.git.add(diffsDict['untracked'])
            if diffsDict['deleted']:
                self.repo.git.add(diffsDict['deleted'])
            if diffsDict['changed']:
                self.repo.git.add(diffsDict['changed'])

    def getStagedFiles(self):
        """Retrieves the files that are already staged ready for commit"""
        return self.repo.index.diff("HEAD")

    def unstageFiles(self, files):
        """Removes changed files from the stage (index) preventing their commit.
        The files in question can be new/changed/deleted
        """
        self.repo.git.reset('--', files)

    def commit(self, message):
        """Commits the staged changes"""
        self.repo.git.commit('-m', message)
        time.sleep(0.1)
        # then get a new copy of the repo
        self.repo = git.Repo(self.localRoot)

    def save(self):
        """Saves the metadata to gitlab.pavlovia.org"""
        self.pavlovia.save()

    @property
    def pavloviaStatus(self):
        return self.__dict__['pavloviaStatus']

    @pavloviaStatus.setter
    def pavloviaStatus(self, newStatus):
        url = 'https://pavlovia.org/server?command=update_project'
        data = {'projectId': self.idNumber, 'projectStatus': 'ACTIVATED'}
        resp = requests.put(url, data)
        if resp.status_code==200:
            self.__dict__['pavloviaStatus'] = newStatus
        else:
            print(resp)


def getGitRoot(p):
    """Return None or the root path of the repository"""
    if not os.path.isdir(p):
        p = os.path.split(p)[0]
    if subprocess.call(["git", "branch"],
                       stderr=subprocess.STDOUT, stdout=open(os.devnull, 'w'),
                       cwd=p) != 0:
        return None
    else:
        out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"],
                                      cwd=p)
        return out.strip().decode('utf-8')


def getProject(filename):
    """Will try to find (locally synced) pavlovia Project for the filename
    """
    gitRoot = getGitRoot(filename)
    if gitRoot in knownProjects:
        return knownProjects[gitRoot]
    elif gitRoot:
        # Existing repo but not in our knownProjects. Investigate
        logging.info("Investigating repo at {}".format(gitRoot))
        localRepo = git.Repo(gitRoot)
        for remote in localRepo.remotes:
            for url in remote.urls:
                if "gitlab.pavlovia.org/" in url:
                    namespaceName = url.split('gitlab.pavlovia.org/')[1]
                    namespaceName = namespaceName.replace('.git', '')
                    pavSession = getCurrentSession()
                    if pavSession.user:
                        try:
                            proj = pavSession.getProject(namespaceName)
                        except gitlab.exceptions.GitlabGetError as e:
                            if "404 Project Not Found" in e.error_message:
                                continue
                        proj.localRoot = gitRoot
                    else:
                        logging.warning(
                            _translate(
                                "We found a git repository pointing to {} but "
                                "no user is logged in for us to chech that "
                                "project")
                                .format(url))
                        return None  # not logged in. Return None
                    return proj
        # if we got here then we have a local git repo but not a
        # TODO: we have a git repo, but not on gitlab.pavlovia so add remote?
        # Could help user that we add a remote called pavlovia but for now
        # just print a message!
        print("We found a git repository at {} but it doesn't point to "
              "gitlab.pavlovia.org. You could create that as a remote to "
              "sync from PsychoPy.")


global _existingSession
_existingSession = None

# create an instance of that
def getCurrentSession():
    """Returns the current Pavlovia session, creating one if not yet present

    Returns
    -------

    """
    global _existingSession
    if _existingSession:
        return _existingSession
    else:
        session = PavloviaSession()
        _existingSession = session
    return _existingSession


class NoUserError(Exception):
    pass

class ConnectionError(Exception):
    pass