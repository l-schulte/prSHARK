"""Handle Github Pull Request fetching over the API and parsing the responses."""
import datetime
import logging
import time
import copy
import requests
import dateutil
from deepdiff import DeepDiff
from prSHARK.utils import process_date
from pycoshark.mongomodels import VCSSystem, Commit, PullRequest, People, PullRequestReview, PullRequestReviewComment, \
    PullRequestComment, PullRequestEvent, PullRequestFile, PullRequestSystem


class Github:
    """
    Github Pull Request API connector

    This is similiar to the issueSHARK github backend only that this fetches pull requests instead of issues.
    However, since every pull request is an issue in github we also fetch events and comments from the issue endpoint.
    """

    def __init__(self, config, project):
        self.config = config
        self._log = logging.getLogger('prSHARK.github')

        self._p = project
        self.last_system_id = None
        self.pr_system = None
        self.parsed_prs = {'prs': {}, 'reviews': {}, 'review_comments': {}, 'comments': {}, 'files': {}, 'events': {}}
        self.old_prs = {'prs': {}, 'reviews': {}, 'review_comments': {}, 'comments': {}, 'files': {}, 'events': {}}
        self.pr_diff = {}
        self.pr_id = None
        self.review_id = None

        self._people = {}  # people cache

    def _send_request(self, url):
        """
        Sends arequest using the requests library to the url specified

        :param url: url to which the request should be sent
        """
        auth = None
        headers = None

        # If tokens are used, set the header, if not use basic authentication
        if self.config.use_token():
            headers = {'Authorization': 'token %s' % self.config.token}
        else:
            auth = requests.auth.HTTPBasicAuth(self.config.issue_user, self.config.issue_password)

        # Make the request
        tries = 1
        while tries <= 3:
            self._log.debug("Sending request to url: %s (Try: %s)", url, tries)
            resp = requests.get(url, headers=headers, proxies=self.config.get_proxy_dictionary(), auth=auth)

            if resp.status_code != 200:
                self._log.error("Problem with getting data via url %s. Code: %s, Error: %s", url, resp.status_code,
                                resp.text)

                # check if we just miss some field, e.g., pulls/{number}/files?&page=1&per_page=100.
                # Error: {"message":"Sorry, there was a problem generating this diff. The repository may be missing relevant data.","errors":[{"resource":"PullRequest","field":"diff","code":"not_available"}],"documentation_url":"https://docs.github.com/v3/pulls#diff-error"}
                if resp.status_code == 422:
                    r = resp.json()
                    if r:
                        if 'errors' in r.keys():
                            for e in r['errors']:
                                if e['resource'] == 'PullRequest' and e['field'] == 'diff' and e[
                                    'code'] == 'not_available' and 'per_page' in url:  # we try to be as explicit as possible here
                                    self._log.error('unfetchable files for pull request, returning [], try: %s', tries)
                                    return []
                if resp.status_code == 500 and tries == 2:  # problem for some part of endpoints, e.g., https://api.github.com/repos/apache/kafka/pulls/3490/reviews/48104142/comments?&page=1&per_page=100
                    self._log.error("Problem with getting data via url %s. Code: %s, Error: %s", url, resp.status_code,
                                    resp.text)
                    if 'per_page' in url:
                        self._log.error('unfetchable list, returning [], try: %s', tries)
                        return []
                tries += 1
                time.sleep(2)
            else:
                # It can happen that we exceed the github api limit. If we have only 1 request left we will wait
                if 'X-RateLimit-Remaining' in resp.headers and int(resp.headers['X-RateLimit-Remaining']) <= 1:
                    # We get the reset time (UTC Epoch seconds)
                    time_when_reset = datetime.datetime.fromtimestamp(float(resp.headers['X-RateLimit-Reset']))
                    now = datetime.datetime.now()

                    # Then we substract and add 10 seconds to it (so that we do not request directly at the threshold
                    waiting_time = ((time_when_reset - now).total_seconds()) + 10

                    self._log.info("Github API limit exceeded. Waiting for %0.5f seconds...", waiting_time)
                    time.sleep(waiting_time)

                    resp = requests.get(url, headers=headers, proxies=self.config.get_proxy_dictionary(), auth=auth)

                self._log.debug('Got response: %s', resp.json())

                return resp.json()

        raise requests.RequestException("Problem with getting data via url %s." % url)

    def _get_repo_url(self, api_url):
        """Changes API url for pull request commit to repository url.
            https://api.github.com/repos/octocat/Hello-World/commits/6dcb09b5b57875f334f61aebed695e2e4193db5e
            ->
            https://github.com/octocat/Hello-World
        """
        url = api_url.replace('https://api.github.com/repos', 'github.com')
        url = '/'.join(url.split('/')[:3])
        return 'https://' + url

    def _get_commit_id(self, revision_hash, repo_url):
        """Links revision_hash and repo_url to existing commits within our MongoDB.

        We require the repo_url because the revision_hash is only unique within one repository.

        :param revision_hash: commit sha for which to fetch the commit id
        :param repo_url: repository url for which to fetch the commit id
        :return: commit_id from MongoDB or None
        """
        commit_id = None
        for vcs in VCSSystem.objects.filter(project_id=self._p.id):
            if repo_url and repo_url not in vcs.url:
                continue
            try:
                commit_id = Commit.objects.get(vcs_system_ids=vcs.id, revision_hash=revision_hash).id
            except Commit.DoesNotExist:  # happens for deleted branches of force pushes, e.g. https://github.com/ravibpatel/AutoUpdater.NET/commit/8dd52654733d688b0111064fd1a841f6769dc082
                pass
        return commit_id

    def _get_person(self, user_url):
        """
        Gets the person via the user url

        :param user_url: url to the github API to get information of the user
        :return: people_id from MongoDB
        """
        # Check if user was accessed before. This reduces the amount of API requests to github
        if user_url in self._people:
            return self._people[user_url]

        # deleted users in github or invalid users in github
        if user_url == 'https://api.github.com/users/invalid-email-address':
            name = 'invalid-email-address'
            login = 'invalid-email-address'
            email = 'null'
        else:
            raw_user = self._send_request(user_url)
            name = raw_user['name']
            login = raw_user['login']

            if name is None:
                name = raw_user['login']

            email = raw_user['email']
            if email is None:
                email = 'null'

        people_id = People.objects(
            name=name,
            email=email
        ).upsert_one(name=name, email=email, username=login).id
        self._people[user_url] = people_id
        return people_id

    def _get_person_without_url(self, name, email):
        """
        Gets the person via the name and email

        Sometimes the github api does not have user urls, in that case we only have a name and email

        :param name: name of the user
        :param email: email of the user
        :return: people_id from MongoDB
        """

        if email is None:
            email = 'null'

        people_id = People.objects(
            name=name,
            email=email
        ).upsert_one(name=name, email=email).id
        return people_id

    def _fetch_all_pages(self, base_url):
        """Convenience method that fetches all available data from all pages.

        :param base_url: url to the github API (should support pagination)
        """
        ret = []
        page = 1
        url = '{}&page={}&per_page=100'.format(base_url, page)
        dat = self._send_request(url)
        ret += dat
        self._log.debug('first page response length %s, return length %s', len(dat), len(ret))

        # early return
        if len(ret) < 100:
            return ret

        # otherwise we want all pages at once
        while len(dat) > 0:
            page += 1
            url = '{}&page={}&per_page=100'.format(base_url, page)
            dat = self._send_request(url)
            ret += dat
            self._log.debug('response length %s, return length %s', len(dat), len(ret))
        return ret

    def run(self):
        """Executes the complete workflow, fetches all data and saves it into the MongoDB."""
        last_system = PullRequestSystem.objects.filter(project_id=self._p.id).order_by('-collection_date').first()
        self.last_system_id = last_system.id if last_system else None

        self.pr_system = PullRequestSystem(project_id=self._p.id, url=self.config.tracking_url, collection_date=datetime.datetime.now())
        self.pr_system.save()

        self.parse_pr_list(self.fetch_pr_list())

        self.save_prs()

    def save_prs(self):
        """
         Save pull requests and related data to the database.

        """
        self._log.info('Saving pull requests')
        for pr_id, pr in self.parsed_prs['prs'].items():
            if pr_id in self.pr_diff and self.pr_diff[pr_id]:
                pr.save()
                if pr_id not in self.parsed_prs['reviews']:
                    continue
                for review_id, review in self.parsed_prs['reviews'][pr_id].items():
                    review.pull_request_id = pr.id
                    review.save()
                    if (pr_id, review_id) not in self.parsed_prs['review_comments']:
                        continue
                    for rc_id, rc in self.parsed_prs['review_comments'][(pr_id, review_id)].items():
                        rc.pull_request_review_id = review.id
                        rc.save()
                for collection in ['comments', 'files', 'events']:
                    if pr_id not in self.parsed_prs[collection]:
                        continue
                    for item_id, item in self.parsed_prs[collection][pr_id].items():
                        item.pull_request_id = pr.id
                        item.save()
            else:
                self.old_prs['prs'][pr_id]['pull_request_system_ids'].append(self.pr_system.id)
                self.old_prs['prs'][pr_id].save()

    def fetch_comment_list(self, pr_number):
        """Pull request comments are extracted via the issues endpoint in Github.
        Each pull request is also an issue in Github.

        :param pr_number: pull request number for which we fetch the issue comments
        """
        base_url = self.config.tracking_url.replace('/pulls', '/issues')
        url = '{}/{}/comments?'.format(base_url, pr_number)
        return self._fetch_all_pages(url)

    def fetch_event_list(self, pr_number):
        """Pull request events are extracted via the issues endpoint in Github.

        :param pr_number: pull request number for which we fetch the issue events
        """
        base_url = self.config.tracking_url.replace('/pulls', '/issues')
        url = '{}/{}/events?'.format(base_url, pr_number)
        return self._fetch_all_pages(url)

    def fetch_timeline_list(self, pr_number):
        """Pull request timeline events are extracted via the issues endpoint in Github.

        :param pr_number: pull request number for which we fetch the issue events
        """
        base_url = self.config.tracking_url.replace('/pulls', '/issues')
        url = '{}/{}/timeline?'.format(base_url, pr_number)
        return self._fetch_all_pages(url)

    def fetch_pr_list(self):
        """Fetch complete list of pull requests for this the url passed on the command line."""
        # this is where we would put since=last_updated_at if it would be supported by the github api
        url = '{}?state=all'.format(
            self.config.tracking_url)
        return self._fetch_all_pages(url)

    def fetch_review_list(self, pr_number):
        """Fetch pull request review

        :param pr_number: pull request number for which to fetch reviews
        """
        url = '{}/{}/reviews?'.format(self.config.tracking_url, pr_number)
        return self._fetch_all_pages(url)

    def fetch_review_comment_list(self, pr_number, review_number):
        """"Fetch pull request review comments

        :param pr_number: pull request number for which to fetch review comments
        :param review_number: review number for which to fetch review comments
        """
        url = '{}/{}/reviews/{}/comments?'.format(self.config.tracking_url, pr_number, review_number)
        return self._fetch_all_pages(url)

    def fetch_commit_list(self, pr_number):
        """Fetch pull request commits

        :param pr_number: pull request number for which to fetch commits
        """
        url = '{}/{}/commits?'.format(self.config.tracking_url, pr_number)
        return self._fetch_all_pages(url)

    def fetch_file_list(self, pr_number):
        """Fetch pull request files

        :param pr_number: pull request number for which to fetch files
        """
        url = '{}/{}/files?'.format(self.config.tracking_url, pr_number)
        return self._fetch_all_pages(url)

    def parse_pr_list(self, prs):
        """Parse response from the Github API.
        We are saving all mongo objects here.
        """
        for pr in prs:
            self._log.info('Parsing pull request %s', pr['number'])
            self.pr_id = str(pr['number'])
            try:
                mongo_pr = PullRequest.objects.get(pull_request_system_ids=self.last_system_id, external_id=str(pr['number']))
                self.old_prs['prs'][self.pr_id] = mongo_pr
            except PullRequest.DoesNotExist:
                mongo_pr = None
            new_pr = PullRequest(pull_request_system_ids=[self.pr_system.id], external_id=str(pr['number']))
            new_pr.title = pr['title']
            new_pr.description = pr['body']
            new_pr.state = pr['state']
            new_pr.is_locked = pr['locked']
            new_pr.lock_reason = pr['active_lock_reason']
            new_pr.is_draft = pr['draft']
            new_pr.created_at = process_date(pr['created_at'])
            new_pr.updated_at = process_date(pr['updated_at'])

            if pr['closed_at']:
                new_pr.closed_at = process_date(pr['closed_at'])

            if pr['merged_at']:
                new_pr.merged_at = process_date(pr['merged_at'])

            if pr['assignee']:
                new_pr.assignee_id = self._get_person(pr['assignee']['url'])

            new_pr.creator_id = self._get_person(pr['user']['url'])
            new_pr.author_association = pr['author_association']

            # head = fork, source of pull
            # base = target of pull, this should be in our data already

            # repo is sometimes null, if it is we can only link to the repository page of the user but that would not be a repo_url
            # also, sometimes even that is not available
            if pr['head']['repo'] and 'full_name' in pr['head']['repo'].keys():
                new_pr.source_repo_url = 'https://github.com/' + pr['head']['repo']['full_name']

            new_pr.source_branch = pr['head']['ref']
            new_pr.source_commit_sha = pr['head']['sha']
            if new_pr.source_repo_url:
                new_pr.source_commit_id = self._get_commit_id(pr['head']['sha'], new_pr.source_repo_url)

            new_pr.target_repo_url = 'https://github.com/' + pr['base']['repo']['full_name']
            new_pr.target_branch = pr['base']['ref']
            new_pr.target_commit_sha = pr['base']['sha']
            new_pr.target_commit_id = self._get_commit_id(pr['base']['sha'], new_pr.target_repo_url)

            new_pr.merge_commit_id = self._get_commit_id(pr['merge_commit_sha'], new_pr.target_repo_url)

            for lbl in pr['labels']:
                if lbl['name'] not in new_pr.labels:
                    new_pr.labels.append(lbl['name'])

            for event in self.fetch_timeline_list(pr['number']):

                if event['event'] == 'committed':
                    new_pr.commits.append(event['sha'])

                elif event['event'] == 'reviewed':

                    self.pares_review(mongo_pr, pr, event)

                elif event['event'] == 'commented':
                    self.parse_comment(event, mongo_pr)

                elif event['event'] == 'assigned':

                    new_pr.linked_user_ids.append(self._get_person(event['assignee']['url']))

                elif event['event'] == 'review_requested':
                    if 'requested_reviewer' in event:
                        new_pr.requested_reviewer_ids.append(self._get_person(event['requested_reviewer']['url']))

            self.parsed_prs['prs'][self.pr_id] = new_pr
            self.check_diff(mongo_pr, new_pr, 'pull_request_system_ids')

            # pr files, sha is not a link to PullRequestCommit, maybe its the file hash
            self.parse_files(mongo_pr, pr)

            # events
            self.parse_events(mongo_pr, pr)

    def parse_events(self, mongo_pr, pr):
        """
           Parse and process events related to a pull request.

           :param mongo_pr: The MongoDB representation of the pull request.
           :param pr: The pull request data from an external source (e.g., GitHub API).
           :return: None
        """
        for e in self.fetch_event_list(pr['number']):
            mongo_pre = None
            if mongo_pr:
                try:
                    mongo_pre = PullRequestEvent.objects.get(pull_request_id=mongo_pr.id, external_id=str(e['id']))
                except PullRequestEvent.DoesNotExist:
                    mongo_pre = None

            new_pre = PullRequestEvent(external_id=str(e['id']))
            if e['actor']:
                new_pre.author_id = self._get_person(e['actor']['url'])

            new_pre.created_at = process_date(e['created_at'])
            new_pre.event_type = e['event']

            if e['commit_id']:
                new_pre.commit_id = self._get_commit_id(e['commit_id'], self._get_repo_url(e['commit_url']))
                new_pre.commit_sha = e['commit_id']
                new_pre.commit_repo_url = self._get_repo_url(e['commit_url'])

            # remove all common attributes then store excess as additional_data
            ad = copy.deepcopy(e)
            del ad['commit_id']
            del ad['event']
            del ad['commit_url']
            del ad['actor']
            del ad['id']
            del ad['node_id']
            del ad['url']
            del ad['created_at']
            new_pre.additional_data = ad

            if self.pr_id not in self.parsed_prs['events']:
                self.parsed_prs['events'][self.pr_id] = {}
            self.parsed_prs['events'][self.pr_id][str(e['id'])] = new_pre
            self.check_diff(mongo_pre, new_pre, 'pull_request_id')

    def parse_files(self, mongo_pr, pr):
        """
        Parse and process files associated with a pull request.

        :param mongo_pr: The MongoDB representation of the pull request.
        :param pr: The pull request data from an external source (e.g., GitHub API).
        """
        for pr_file in self.fetch_file_list(pr['number']):
            mongo_pr_file = None
            if mongo_pr:
                try:
                    mongo_pr_file = PullRequestFile.objects.get(pull_request_id=mongo_pr.id, path=pr_file['filename'])
                except PullRequestFile.DoesNotExist:
                    mongo_pr_file = None
            new_pr_file = PullRequestFile(path=pr_file['filename'])
            new_pr_file.sha = pr_file['sha']
            new_pr_file.status = pr_file['status']
            new_pr_file.additions = pr_file['additions']
            new_pr_file.deletions = pr_file['deletions']
            new_pr_file.changes = pr_file['changes']
            if 'patch' in pr_file.keys():
                new_pr_file.patch = pr_file['patch']

            if self.pr_id not in self.parsed_prs['files']:
                self.parsed_prs['files'][self.pr_id] = {}
            self.parsed_prs['files'][self.pr_id][pr_file['filename']] = new_pr_file
            self.check_diff(mongo_pr_file, new_pr_file, 'pull_request_id')

    def parse_comment(self, c, mongo_pr):
        """
        Parses and stores a comment from a pull request in the database.


        :param c: A dictionary containing comment information from the pull request.
        :param mongo_pr:The associated pull request in the MongoDB.
        :return: None
        """
        mongo_prc = None
        if mongo_pr:
            try:
                mongo_prc = PullRequestComment.objects.get(pull_request_id=mongo_pr.id, external_id=str(c['id']))
            except PullRequestComment.DoesNotExist:
                mongo_prc = None

        new_prc = PullRequestComment(external_id=str(c['id']))
        new_prc.created_at = process_date(c['created_at'])
        new_prc.updated_at = process_date(c['updated_at'])
        new_prc.author_id = self._get_person(c['user']['url'])
        new_prc.comment = c['body']
        new_prc.author_association = c['author_association']

        if self.pr_id not in self.parsed_prs['comments']:
            self.parsed_prs['comments'][self.pr_id] = {}
        self.parsed_prs['comments'][self.pr_id][str(c['id'])] = new_prc
        self.check_diff(mongo_prc, new_prc, 'pull_request_id')

    def pares_review(self, mongo_pr, pr, prr):
        """
        Parses and stores a pull request review in the database, along with its associated review comments.


        :param mongo_pr: The associated pull request in the MongoDB.
        :param pr: A dictionary containing pull request information.
        :param prr: A dictionary containing pull request review information.
        :return: None.
        """
        self.review_id = str(prr['id'])
        mongo_prr = None
        if mongo_pr:
            try:
                mongo_prr = PullRequestReview.objects.get(pull_request_id=mongo_pr.id, external_id=str(prr['id']))
            except PullRequestReview.DoesNotExist:
                mongo_prr = None

        new_prr = PullRequestReview(external_id=str(prr['id']))

        new_prr.state = prr['state']
        new_prr.description = prr['body']
        new_prr.submitted_at = process_date(prr['submitted_at'])
        if 'commit_id' in prr.keys():
            new_prr.commit_sha = prr['commit_id']
        if prr['user']:
            new_prr.creator_id = self._get_person(prr['user']['url'])
        new_prr.author_association = prr['author_association']

        if self.pr_id not in self.parsed_prs['reviews']:
            self.parsed_prs['reviews'][self.pr_id] = {}
        self.parsed_prs['reviews'][self.pr_id][self.review_id] = new_prr
        self.check_diff(mongo_prr, new_prr, 'pull_request_id')


        # pul   l request review comment
        for prrc in self.fetch_review_comment_list(pr['number'], prr['id']):
            self.parse_review_comment(mongo_pr, mongo_prr, prrc)

    def parse_review_comment(self, mongo_pr, mongo_prr, prrc):
        """
        Parses and stores a review comment from a pull request review in the database.


        :param mongo_pr: The associated pull request in the MongoDB.
        :param mongo_prr: The associated pull request review in the MongoDB.
        :param prrc: A dictionary containing review comment information.
        :return: None.

        """
        mongo_prrc = None
        if mongo_prr:
            try:
                mongo_prrc = PullRequestReviewComment.objects.get(pull_request_review_id=mongo_prr.id,
                                                                  external_id=str(prrc['id']))
            except PullRequestReviewComment.DoesNotExist:
                mongo_prrc = None

        new_prrc = PullRequestReviewComment(external_id=str(prrc['id']))
        new_prrc.diff_hunk = prrc['diff_hunk']
        # we do not link to PullRequestFile here because
        # PullRequestFile is sometimes missing, maybe the file is no longer part of current file list after a commit to the pull request
        new_prrc.path = prrc['path']
        new_prrc.position = prrc['position']
        new_prrc.original_position = prrc['original_position']
        new_prrc.comment = prrc['body']
        if prrc['user']:
            new_prrc.creator_id = self._get_person(prrc['user']['url'])
        new_prrc.created_at = process_date(prrc['created_at'])
        new_prrc.updated_at = process_date(prrc['updated_at'])
        new_prrc.author_association = prrc['author_association']
        new_prrc.commit_sha = prrc['commit_id']
        new_prrc.original_commit_sha = prrc['original_commit_id']
        # some recent additions to the api, may not be available yet
        if 'start_line' in prrc.keys():
            new_prrc.start_line = prrc['start_line']
        if 'original_start_line' in prrc.keys():
            new_prrc.original_start_line = prrc['original_start_line']
        if 'start_side' in prrc.keys():
            new_prrc.start_side = prrc['start_side']
        if 'line' in prrc.keys():
            new_prrc.line = prrc['line']
        if 'original_line' in prrc.keys():
            new_prrc.original_line = prrc['original_line']
        if 'side' in prrc.keys():
            new_prrc.side = prrc['side']

        if 'in_reply_to_id' in prrc:
            new_prrc.in_reply_to_id = str(prrc['in_reply_to_id'])

        if (self.pr_id, self.review_id) not in self.parsed_prs['review_comments']:
            self.parsed_prs['review_comments'][(self.pr_id, self.review_id)] = {}
        self.parsed_prs['review_comments'][(self.pr_id, self.review_id)][str(prrc['id'])] = new_prrc
        self.check_diff(mongo_prrc, new_prrc, 'pull_request_review_id')

    def check_diff(self, old, new, ex_path):
        """
        Compare and identify differences between old and new objects.

        This function compares two objects, `old` and `new`, typically representing data from different
        states, to identify differences between them. The comparison is performed by utilizing the
        DeepDiff library. Differences are stored in the `pr_diff` dictionary for the pull request
        identified by `self.pr_id`. If differences are found, the corresponding key in `pr_diff`
        is set to `True`.

        :param old: The old object to be compared.
        :param new: The new object to be compared.
        :param ex_path: List of paths to be excluded from the comparison.

        """
        if self.pr_id not in self.pr_diff:
            self.pr_diff[self.pr_id] = False

        if old:
            diff = DeepDiff(t1=old.to_mongo().to_dict(), t2=new.to_mongo().to_dict(), exclude_paths=['_id', ex_path])
            if diff:
                self.pr_diff[self.pr_id] = True
        else:
            self.pr_diff[self.pr_id] = True
