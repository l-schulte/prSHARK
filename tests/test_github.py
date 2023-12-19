"""Tests for the github backend"""

import datetime
import pprint
import unittest
import json
from unittest.mock import patch
from argparse import Namespace

import mongoengine

from pycoshark.mongomodels import Project, VCSSystem, Commit, PullRequestSystem, PullRequest, People, PullRequestReview, PullRequestReviewComment, PullRequestComment, PullRequestEvent, PullRequestCommit, PullRequestFile
from reviewSHARK.backends.github import Github

# load simple fixtures (taken from the github api examples)
with open('tests/fixtures/user.json') as f:
    person = json.loads(f.read())

with open('tests/fixtures/user2.json') as f:
    person2 = json.loads(f.read())

with open('tests/fixtures/user3.json') as f:
    person3 = json.loads(f.read())

with open('tests/fixtures/pr_list.json', 'r') as f:
    pr_list = json.loads(f.read())

with open('tests/fixtures/pr_reviews.json', 'r') as f:
    review_list = json.loads(f.read())

with open('tests/fixtures/pr_review_comments.json', 'r') as f:
    review_comment_list = json.loads(f.read())

with open('tests/fixtures/issue_comments.json', 'r') as f:
    issue_comment_list = json.loads(f.read())

with open('tests/fixtures/issue_events.json', 'r') as f:
    issue_event_list = json.loads(f.read())

with open('tests/fixtures/pr_commits.json', 'r') as f:
    pr_commit_list = json.loads(f.read())

with open('tests/fixtures/pr_files.json', 'r') as f:
    pr_file_list = json.loads(f.read())


def mock_return(*args, **kwargs):
    url = args[0].split('?')[0]
    if 'user' in url and 'octocat' in url:
        return person
    if 'user' in url and 'hubot' in url:
        return person2
    if 'user' in url and 'other_user' in url:
        return person3

    if url.endswith('/pulls'):
        return pr_list
    if '/pulls/' in url and url.endswith('/reviews'):
        return review_list
    if '/pulls/' in url and url.endswith('/comments'):
        return review_comment_list
    if '/issues/' in url and url.endswith('/comments'):
        return issue_comment_list
    if '/issues/' in url and url.endswith('/events'):
        return issue_event_list
    if '/pulls/' in url and url.endswith('/commits'):
        return pr_commit_list
    if '/pulls/' in url and url.endswith('/files'):
        return pr_file_list


class TestGithubBackend(unittest.TestCase):
    """Test Github Backend."""

    def setUp(self):
        """Setup the mongomock connection."""
        mongoengine.connection.disconnect()
        mongoengine.connect('testdb', host='mongomock://localhost')
        p = Project(name='test')
        p.save()

        pr_system = PullRequestSystem(project_id=p.id, url='https://localhost/repos/smartshark/test/pulls')
        pr_system.save()

    def tearDown(self):
        """Tear down the mongomock connection."""
        mongoengine.connection.disconnect()

    @patch('reviewSHARK.backends.github.Github._send_request', side_effect=mock_return)
    def test_pr_list(self, mock_request):
        """Test Github Parser pull request list response parsing."""

        cfg = Namespace(tracking_url='https://localhost/repos/smartshark/test/pulls')

        project = Project.objects.get(name='test')
        pr_system = PullRequestSystem.objects.get(project_id=project.id)

        vcs_system = VCSSystem(project_id=project.id,
                               url='https://github.com/octocat/Hello-World.git', repository_type='git')
        vcs_system.save()

        c = Commit(vcs_system_id=vcs_system.id, revision_hash='6dcb09b5b57875f334f61aebed695e2e4193db5e')
        c.save()

        mc = Commit(vcs_system_id=vcs_system.id, revision_hash='e5bd3914e2e596debea16f433f57875b5b90bcd6')
        mc.save()

        with open('tests/fixtures/pr_list.json', 'r') as fi:
            data = json.loads(fi.read())

        pr1 = data[0]
        gp = Github(cfg, project, pr_system)
        gp.parse_pr_list(data)

        pr = PullRequest.objects.get(external_id='1347')
        p = People.objects.get(id=pr.assignee_id)
        h = People.objects.get(name='mr hubot')
        o = People.objects.get(name='other user')
        prr = PullRequestReview.objects.get(pull_request_id=pr.id)
        prrc = PullRequestReviewComment.objects.get(pull_request_review_id=prr.id, external_id='10')

        prc = PullRequestComment.objects.get(pull_request_id=pr.id)
        pre = PullRequestEvent.objects.get(pull_request_id=pr.id)

        prcc = PullRequestCommit.objects.get(pull_request_id=pr.id)
        prccf = PullRequestFile.objects.get(pull_request_id=pr.id)

        self.assertEqual(p.name, 'monalisa octocat')
        self.assertEqual(h.name, 'mr hubot')
        self.assertEqual(o.name, 'other user')

        # pull request
        self.assertEqual(pr.title, pr1['title'])
        self.assertEqual(pr.description, pr1['body'])
        self.assertEqual(pr.state, pr1['state'])
        self.assertEqual(pr.is_locked, pr1['locked'])
        self.assertEqual(pr.is_draft, pr1['draft'])
        self.assertEqual(pr.lock_reason, pr1['active_lock_reason'])
        self.assertEqual(pr.labels, [pr1['labels'][0]['name']])
        self.assertEqual(pr.author_association, pr1['author_association'])

        self.assertEqual(pr.created_at, datetime.datetime(2011, 1, 26, 19, 1, 12))
        self.assertEqual(pr.updated_at, datetime.datetime(2011, 1, 26, 19, 1, 12))
        self.assertEqual(pr.merged_at, datetime.datetime(2011, 1, 26, 19, 1, 12))
        self.assertEqual(pr.merge_commit_id, mc.id)
        self.assertEqual(pr.creator_id, p.id)
        self.assertEqual(pr.assignee_id, p.id)
        self.assertEqual(pr.linked_user_ids, [p.id, h.id])
        self.assertEqual(pr.requested_reviewer_ids, [o.id])
        self.assertEqual(pr.source_repo_url, 'https://github.com/octocat/Hello-World')
        self.assertEqual(pr.source_branch, 'new-topic')
        self.assertEqual(pr.source_commit_sha, '6dcb09b5b57875f334f61aebed695e2e4193db5e')

        self.assertEqual(pr.target_repo_url, 'https://github.com/octocat/Hello-World')
        self.assertEqual(pr.target_branch, 'master')
        self.assertEqual(pr.target_commit_sha, '6dcb09b5b57875f334f61aebed695e2e4193db5e')

        # pull request review
        self.assertEqual(prr.state, 'APPROVED')
        self.assertEqual(prr.description, 'Here is the body for the review.')
        self.assertEqual(prr.submitted_at, datetime.datetime(2019, 11, 17, 17, 43, 43))
        self.assertEqual(prr.author_association, 'collaborator')
        self.assertEqual(prr.creator_id, p.id)
        self.assertEqual(prr.commit_sha, 'ecdd80bb57125d7ba9641ffaa4d7d2c19d3f3091')

        # pull request review comment
        self.assertEqual(prrc.comment, 'Great stuff!')
        self.assertEqual(prrc.path, 'file1.txt')
        self.assertEqual(prrc.position, 1)
        self.assertEqual(prrc.original_position, 4)
        self.assertEqual(prrc.diff_hunk, '@@ -16,33 +16,40 @@ public class Connection : IConnection...')
        self.assertEqual(prrc.created_at, datetime.datetime(2011, 4, 14, 16, 0, 49))
        self.assertEqual(prrc.updated_at, datetime.datetime(2011, 4, 14, 16, 0, 49))
        self.assertEqual(prrc.author_association, "NONE")
        self.assertEqual(prrc.creator_id, p.id)
        self.assertEqual(prrc.commit_sha, '6dcb09b5b57875f334f61aebed695e2e4193db5e')
        self.assertEqual(prrc.original_commit_sha, '9c48853fa3dc5c1c3d6f1f1cd1f2743e72652840')

        self.assertEqual(prrc.start_line, 1)
        self.assertEqual(prrc.original_start_line, 1)
        self.assertEqual(prrc.start_side, 'RIGHT')
        self.assertEqual(prrc.line, 2)
        self.assertEqual(prrc.original_line, 2)
        self.assertEqual(prrc.side, 'RIGHT')

        # everthing for the pull request comment (not review comment!)
        self.assertEqual(prc.comment, 'Me too')
        self.assertEqual(prc.author_id, p.id)
        self.assertEqual(prc.created_at, datetime.datetime(2011, 4, 14, 16, 0, 49))
        self.assertEqual(prc.updated_at, datetime.datetime(2011, 4, 14, 16, 0, 49))
        self.assertEqual(prc.author_association, 'collaborator')

        # evertyhing for the event
        self.assertEqual(pre.event_type, 'closed')
        self.assertEqual(pre.commit_repo_url, 'https://github.com/octocat/Hello-World')
        self.assertEqual(pre.commit_sha, '6dcb09b5b57875f334f61aebed695e2e4193db5e')
        self.assertEqual(pre.commit_id, c.id)
        self.assertEqual(pre.created_at, datetime.datetime(2011, 4, 14, 16, 0, 49))

        # everything for the commit
        author_id = People.objects.get(name='monalisa octocat').id
        self.assertEqual(prcc.commit_sha, '6dcb09b5b57875f334f61aebed695e2e4193db5e')
        self.assertEqual(prcc.commit_repo_url, 'https://github.com/octocat/Hello-World')
        self.assertEqual(prcc.message, "Fix all the bugs")
        self.assertEqual(prcc.author_id, author_id)
        self.assertEqual(prcc.committer_id, author_id)
        self.assertEqual(prcc.commit_id, c.id)  # should equal our created Commit above
        self.assertEqual(prcc.parents, ['6dcb09b5b57875f334f61aebed695e2e4193db5e'])

        # everything for the file
        self.assertEqual(prccf.sha, 'bbcd538c8e72b8c175046e27cc8f907076331401')
        self.assertEqual(prccf.path, 'file1.txt')
        self.assertEqual(prccf.status, 'added')
        self.assertEqual(prccf.additions, 103)
        self.assertEqual(prccf.deletions, 21)
        self.assertEqual(prccf.changes, 103 + 21)
        self.assertEqual(prccf.patch, "@@ -132,7 +132,7 @@ module Test @@ -1000,7 +1000,7 @@ module Test")
