"""Handle Github Pull Request fetching over the API and parsing the responses."""
import datetime
import logging
import time
import copy
import requests
import dateutil

from pycoshark.mongomodels import VCSSystem, Commit, PullRequest, People, PullRequestReview, PullRequestReviewComment, PullRequestComment, PullRequestEvent, PullRequestCommit, PullRequestFile


class Github():
    """Github Pull Request API connector

    This is similiar to the issueSHARK github backend only that this fetches pull requests instead of issues.
    However, since every pull request is an issue in github we also fetch events and comments from the issue endpoint.
    """

    def __init__(self, config, project, pull_request_system):
        self.config = config
        self._log = logging.getLogger('prSHARK.github')

        self._prs = pull_request_system
        self._p = project

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
                self._log.error("Problem with getting data via url %s. Error: %s", url, resp.text)
                tries += 1
                time.sleep(2)
            else:
                # It can happen that we exceed the github api limit. If we have only 1 request left we will wait
                if 'X-RateLimit-Remaining' in resp.headers and int(resp.headers['X-RateLimit-Remaining']) <= 1:

                    # We get the reset time (UTC Epoch seconds)
                    time_when_reset = datetime.datetime.fromtimestamp(float(resp.headers['X-RateLimit-Reset']))
                    now = datetime.datetime.now()

                    # Then we substract and add 10 seconds to it (so that we do not request directly at the threshold
                    waiting_time = ((time_when_reset-now).total_seconds())+10

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

    def _get_person(self, user_url):
        """
        Gets the person via the user url

        :param user_url: url to the github API to get information of the user
        """
        # Check if user was accessed before. This reduces the amount of API requests to github
        if user_url in self._people:
            return self._people[user_url]

        raw_user = self._send_request(user_url)
        name = raw_user['name']

        if name is None:
            name = raw_user['login']

        email = raw_user['email']
        if email is None:
            email = 'null'

        people_id = People.objects(
            name=name,
            email=email
        ).upsert_one(name=name, email=email, username=raw_user['login']).id
        self._people[user_url] = people_id
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

        # early return
        if len(ret) < 100:
            return ret

        # otherwise we want all pages at once
        while len(dat) > 0:
            page += 1
            url = '{}&page={}&per_page=100'.format(base_url, page)
            dat = self._send_request(url)
            ret += dat
        return dat

    def run(self):
        """Executes the complete workflow, fetches all data and saves it into the MongoDB."""
        self.parse_pr_list(self.fetch_pr_list())

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

    def fetch_pr_list(self):
        """Fetch complete list of pull requests for this the url passed on the command line."""
        url = '{}?'.format(self.config.tracking_url)  # this is where we would put since=last_updated_at if it would be supported by the github api
        return self._fetch_all_pages(url)

    def fetch_review_list(self, pr_number):
        """Fetch pull request reviews
        
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
            self._log.info('saving pull request %s', pr['number'])
            try:
                mongo_pr = PullRequest.objects.get(pull_request_system_id=self._prs.id, external_id=str(pr['number']))
            except PullRequest.DoesNotExist:
                mongo_pr = PullRequest(pull_request_system_id=self._prs.id, external_id=str(pr['number']))

            mongo_pr.title = pr['title']
            mongo_pr.description = pr['body']
            mongo_pr.state = pr['state']
            mongo_pr.is_locked = pr['locked']
            mongo_pr.lock_reason = pr['active_lock_reason']
            mongo_pr.is_draft = pr['draft']
            mongo_pr.created_at = dateutil.parser.parse(pr['created_at'])
            mongo_pr.updated_at = dateutil.parser.parse(pr['updated_at'])

            mongo_pr.merge_commit_id = self._get_commit_id(pr['merge_commit_sha'])
            if pr['closed_at']:
                mongo_pr.closed_at = dateutil.parser.parse(pr['closed_at'])

            if pr['merged_at']:
                mongo_pr.merged_at = dateutil.parser.parse(pr['merged_at'])

            if pr['assignee']:
                mongo_pr.assignee_id = self._get_person(pr['assignee']['url'])

            mongo_pr.creator_id = self._get_person(pr['user']['url'])
            mongo_pr.author_association = pr['author_association']

            # head = fork, source of pull
            # base = target of pull, this should be in our data already

            mongo_pr.source_repo_url = 'https://github.com/' + pr['head']['repo']['full_name']
            mongo_pr.source_branch = pr['head']['ref']
            mongo_pr.source_commit_sha = pr['head']['sha']
            mongo_pr.source_commit_id = self._get_commit_id(pr['head']['sha'], pr['head']['repo']['full_name'])

            mongo_pr.target_repo_url = 'https://github.com/' + pr['base']['repo']['full_name']
            mongo_pr.target_branch = pr['base']['ref']
            mongo_pr.target_commit_sha = pr['base']['sha']
            mongo_pr.target_commit_id = self._get_commit_id(pr['base']['sha'], pr['base']['repo']['full_name'])

            for u in pr['assignees']:
                mongo_pr.linked_user_ids.append(self._get_person(u['url']))

            for u in pr['requested_reviewers']:
                mongo_pr.requested_reviewer_ids.append(self._get_person(u['url']))

            for lbl in pr['labels']:
                mongo_pr.labels.append(lbl['name'])
            mongo_pr.save()

            # pull request reviews
            for prr in self.fetch_review_list(pr['number']):
                try:
                    mongo_prr = PullRequestReview.objects.get(pull_request_id=mongo_pr.id, external_id=str(prr['id']))
                except PullRequestReview.DoesNotExist:
                    mongo_prr = PullRequestReview(pull_request_id=mongo_pr.id, external_id=str(prr['id']))

                mongo_prr.state = prr['state']
                mongo_prr.description = prr['body']
                mongo_prr.submitted_at = dateutil.parser.parse(prr['submitted_at'])
                mongo_prr.commit_sha = prr['commit_id']
                mongo_prr.creator_id = self._get_person(prr['user']['url'])
                mongo_prr.author_association = prr['author_association']
                mongo_prr.save()

                # pull request review comment
                for prrc in self.fetch_review_comment_list(pr['number'], prr['id']):
                    try:
                        mongo_prrc = PullRequestReviewComment.objects.get(pull_request_review_id=mongo_prr.id, external_id=str(prrc['id']))
                    except PullRequestReviewComment.DoesNotExist:
                        mongo_prrc = PullRequestReviewComment(pull_request_review_id=mongo_prr.id, external_id=str(prrc['id']))

                    mongo_prrc.diff_hunk = prrc['diff_hunk']
                    mongo_prrc.path = prrc['path']
                    mongo_prrc.position = prrc['position']
                    mongo_prrc.original_position = prrc['original_position']
                    mongo_prrc.comment = prrc['body']

                    mongo_prrc.creator_id = self._get_person(prrc['user']['url'])
                    mongo_prrc.created_at = dateutil.parser.parse(prrc['created_at'])
                    mongo_prrc.updated_at = dateutil.parser.parse(prrc['updated_at'])
                    mongo_prrc.author_association = prrc['author_association']

                    mongo_prrc.commit_sha = prrc['commit_id']
                    mongo_prrc.original_commit_sha = prrc['original_commit_id']

                    mongo_prrc.start_line = prrc['start_line']
                    mongo_prrc.original_start_line = prrc['original_start_line']
                    mongo_prrc.start_side = prrc['start_side']
                    mongo_prrc.line = prrc['line']
                    mongo_prrc.original_line = prrc['original_line']
                    mongo_prrc.side = prrc['side']

                    if 'in_reply_to_id' in prrc.keys() and prrc['in_reply_to_id']:
                        try:
                            ref_prrc = PullRequestReviewComment.objects.get(pull_request_review_id=mongo_prr.id, external_id=str(prrc['in_reply_to_id']))
                        except PullRequestReviewComment.DoesNotExist:
                            ref_prrc = PullRequestReviewComment(pull_request_review_id=mongo_prr.id, external_id=str(prrc['in_reply_to_id']))
                            ref_prrc.save()  # create empty for ref, will get populated later
                        mongo_prrc.in_reply_to_id = ref_prrc.id
                    mongo_prrc.save()

            # pr commits
            for pr_commit in self.fetch_commit_list(pr['number']):
                try:
                    mongo_pr_commit = PullRequestCommit.objects.get(pull_request_id=mongo_pr.id, commit_sha=pr_commit['sha'])
                except PullRequestCommit.DoesNotExist:
                    mongo_pr_commit = PullRequestCommit(pull_request_id=mongo_pr.id, commit_sha=pr_commit['sha'])

                mongo_pr_commit.author_id = self._get_person(pr_commit['author']['url'])
                mongo_pr_commit.committer_id = self._get_person(pr_commit['committer']['url'])
                mongo_pr_commit.parents = [p['sha'] for p in pr_commit['parents']]
                mongo_pr_commit.message = pr_commit['commit']['message']
                mongo_pr_commit.commit_repo_url = self._get_repo_url(pr_commit['url'])
                mongo_pr_commit.commit_id = self._get_commit_id(mongo_pr_commit.commit_sha, mongo_pr_commit.commit_repo_url)
                mongo_pr_commit.save()

            # pr files, sha is not a link to PullRequestCommit, maybe its the file hash
            for pr_file in self.fetch_file_list(pr['number']):
                try:
                    mongo_pr_file = PullRequestFile.objects.get(pull_request_id=mongo_pr.id, sha=pr_file['sha'], path=pr_file['filename'])
                except PullRequestFile.DoesNotExist:
                    mongo_pr_file = PullRequestFile(pull_request_id=mongo_pr.id, sha=pr_file['sha'], path=pr_file['filename'])

                mongo_pr_file.status = pr_file['status']
                mongo_pr_file.additions = pr_file['additions']
                mongo_pr_file.deletions = pr_file['deletions']
                mongo_pr_file.changes = pr_file['changes']
                mongo_pr_file.patch = pr_file['patch']
                mongo_pr_file.save()

            # comments outside of reviews
            for c in self.fetch_comment_list(pr['number']):
                try:
                    mongo_prc = PullRequestComment.objects.get(pull_request_id=mongo_pr.id, external_id=str(c['id']))
                except PullRequestComment.DoesNotExist:
                    mongo_prc = PullRequestComment(pull_request_id=mongo_pr.id, external_id=str(c['id']))

                mongo_prc.created_at = dateutil.parser.parse(c['created_at'])
                mongo_prc.updated_at = dateutil.parser.parse(c['updated_at'])
                mongo_prc.author_id = self._get_person(c['user']['url'])
                mongo_prc.comment = c['body']
                mongo_prc.author_association = c['author_association']
                mongo_prc.save()

            # events
            for e in self.fetch_event_list(pr['number']):
                try:
                    mongo_pre = PullRequestEvent.objects.get(pull_request_id=mongo_pr.id, external_id=str(e['id']))
                except PullRequestEvent.DoesNotExist:
                    mongo_pre = PullRequestEvent(pull_request_id=mongo_pr.id, external_id=str(e['id']))

                mongo_pre.author_id = self._get_person(e['actor']['url'])
                mongo_pre.created_at = dateutil.parser.parse(e['created_at'])
                mongo_pre.event_type = e['event']
                
                if e['commit_id']:
                    print(e['commit_id'], 'in', self._get_repo_url(e['url']))
                    mongo_pre.commit_id = self._get_commit_id(e['commit_id'], self._get_repo_url(e['url']))
                    mongo_pre.commit_sha = e['commit_id']
                    mongo_pre.commit_repo_url = self._get_repo_url(e['url'])

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
                mongo_pre.additional_data = ad
                mongo_pre.save()

    def _get_commit_id(self, revision_hash, repo_url=None):
        commit_id = None
        for vcs in VCSSystem.objects.filter(project_id=self._p.id):
            if repo_url and repo_url not in vcs.url:
                continue
            try:
                commit_id = Commit.objects.get(vcs_system_id=vcs.id, revision_hash=revision_hash).id
            except Commit.DoesNotExist:
                pass
        return commit_id