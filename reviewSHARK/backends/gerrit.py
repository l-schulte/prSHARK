import logging
import typing
import requests
import json
import dateutil
import time

from mongoengine.errors import DoesNotExist
from pycoshark.mongomodels import (
    CodeReview,
    CodeReviewChangeLog,
    CodeReviewComment,
    CodeReviewRevision,
    People,
    CodeReviewSystem,
    Issue,
)


def elvis(dict, key, fallback_value=None):
    """
    Returns the value of the key in the dict or the fallback_value if the key is not in the dict.
    :param dict:
    :param key:
    :param fallback_value:
    """
    return dict[key] if (dict is not None and key in dict and dict[key] is not None) else fallback_value


def parse_date(date_string):
    """
    Parses a date string into a datetime object.
    :param date_string:
    :return:
    """
    return dateutil.parser.parse(date_string) if date_string is not None else None


class Gerrit:
    """Gerrit code review API connector

    Populates the smartSHARK backend with code review data from Gerrit.
    """

    # people cache
    people_id_cache: dict = {}

    # revision id cache
    revision_id_cache: dict = {}

    def __init__(self, config, project, review_system: CodeReviewSystem):
        self.config = config
        self._log = logging.getLogger("reviewSHARK.github")

        self.project = project
        self.review_system = review_system

        self.review_system.type = "gerrit"
        self.review_system.save()

        self.base_url = config.tracking_url

    def run(self):
        """Executes the complete workflow"""

        for raw_review in self.get_reviews():
            raw_review_id = raw_review["id"]

            # store review
            review = self.store_review(raw_review)

            revision_info: list[(str, str)] = [
                (key, item["commit"]["parents"][0]["commit"]) for key, item in raw_review["revisions"].items()
            ]

            # get and store revisions
            for revision_external_id, commit_hash in revision_info:
                raw_revision = self.get_revision(raw_review_id, revision_external_id, commit_hash)
                revision = self.store_revision(raw_revision, review.id)
                review.revisions.append(revision.id)

            # get and store change logs
            raw_change_logs = self.get_change_logs(raw_review_id)
            change_logs = self.store_change_logs(raw_change_logs, review.id)

            # get and store comments
            raw_comments = self.get_comments(raw_review_id)
            comments = self.store_comments(raw_comments, review.id)

            review.save()

    def get_reviews(self) -> typing.Generator:
        """Fetches all reviews for the project"""

        project_name = self.config.alternate_project_name if self.config.alternate_project_name else self.project.name

        url = self.base_url + "/changes/"  # "https://review.opendev.org/changes?q=repo:openstack/nova"

        raw_reviews = []

        next_page = True
        while next_page:
            data = self._make_request(
                url,
                params={
                    "start": len(raw_reviews),
                    "q": f"repo:{project_name}",
                    "o": ["ALL_REVISIONS", "DETAILED_ACCOUNTS", "ALL_COMMITS", "SKIP_DIFFSTAT"],
                },
            )
            for review in data:
                yield review
            next_page = elvis(data[-1], "_more_changes", False)

    def store_review(self, raw_review) -> CodeReview:
        """Stores a review in the database"""

        try:
            review = CodeReview.objects.get(external_id=raw_review["id"])
        except DoesNotExist:
            review = CodeReview(external_id=raw_review["id"])

        review.code_review_system_ids = [self.review_system.id]
        review.external_id = raw_review["id"]
        review.external_number = raw_review["_number"]

        review.revisions = []

        review.title = raw_review["subject"]
        review.labels = elvis(raw_review, "hashtags")

        review.change_id = raw_review["change_id"]
        review.topic = elvis(raw_review, "topic")
        review.linked_issue_id = self._get_issue_id_from_topic(review.topic)
        review.author_id = self._get_people_id(raw_review["owner"])
        review.submitter_id = self._get_people_id(elvis(raw_review, "submitter"))

        review.status = raw_review["status"]
        review.review_started = raw_review["has_review_started"]
        review.created_at = parse_date(raw_review["created"])
        review.updated_at = parse_date(raw_review["updated"])
        review.submitted_at = parse_date(raw_review["submitted"]) if "submitted" in raw_review else None
        review.mergable = elvis(raw_review, "mergeable")
        review.current_revision_commit_hash = raw_review["current_revision"]

        # review.more = {}

        return review.save()

    def _get_issue_id_from_topic(self, topic) -> Issue:
        """Fetches the issue based on the id extracted from the topic.

        Assumes that the topic is in the format: "bug/1234" or "bp/name-of-task".
        """

        if not topic or "/" not in topic:
            return None

        issue_external_id = topic.split("/")[-1]

        try:
            return Issue.objects.get(external_id=issue_external_id).id
        except DoesNotExist:
            return None

    def get_change_logs(self, code_review_external_id) -> list[dict]:
        """Fetches the change log for the code review"""

        url = self.base_url + "/changes/" + code_review_external_id + "/messages"

        return self._make_request(url)

    def store_change_logs(self, raw_change_logs, code_review_id) -> list[CodeReviewChangeLog]:
        """Stores a change log in the database"""

        change_logs = []
        save_list = []

        for raw_change_log in raw_change_logs:
            try:
                change_log = CodeReviewChangeLog.objects.get(external_id=raw_change_log["id"])
                change_logs.append(change_log)
            except DoesNotExist:
                change_log = CodeReviewChangeLog(external_id=raw_change_log["id"])

                change_log.code_review_id = code_review_id
                change_log.external_id = raw_change_log["id"]

                change_log.revision_id = self._get_revision_id(code_review_id, raw_change_log["_revision_number"])

                change_log.author_id = self._get_people_id(raw_change_log["author"])
                change_log.message = raw_change_log["message"]

                change_log.created_at = parse_date(raw_change_log["date"])

                change_log.mpre = raw_change_log["accounts_in_message"]

                save_list.append(change_log)

        if len(save_list) > 0:
            change_logs.extend(CodeReviewChangeLog.objects.insert(save_list))

        return change_logs

    def get_revision(self, code_review_external_id, revision_external_id, commit_hash) -> dict:
        """Fetches all revisions for the code review"""

        base_url = self.base_url + "/changes/" + code_review_external_id + "/revisions/" + revision_external_id

        raw_revision_review = self._make_request(base_url + "/review")
        revision_description = self._make_request(base_url + "/description")

        raw_revision_review["revision_external_id"] = revision_external_id
        raw_revision_review["commit"] = commit_hash
        raw_revision_review["description"] = revision_description

        return raw_revision_review

    def store_revision(self, raw_revision, code_review_id) -> CodeReviewRevision:
        """Stores a revision in the database"""

        try:
            revision = CodeReviewRevision.objects.get(external_id=raw_revision["revision_external_id"])
        except DoesNotExist:
            revision = CodeReviewRevision(external_id=raw_revision["revision_external_id"])

        revision.code_review_id = code_review_id
        revision.external_id = raw_revision["revision_external_id"]

        revision.revision_number = raw_revision["revisions"][raw_revision["revision_external_id"]]["_number"]

        revision.author_id = self._get_people_id(raw_revision["owner"])
        revision.submitter_id = self._get_people_id(elvis(raw_revision, "submitter"))
        revision.created_at = parse_date(raw_revision["created"])
        revision.updated_at = parse_date(raw_revision["updated"])
        revision.submitted_at = parse_date(elvis(raw_revision, "submitted"))

        revision.description = raw_revision["description"]
        revision.commit_hash = raw_revision["commit"]

        revision.reviewer_ids = [
            self._get_people_id(raw_reviewer) for raw_reviewer in elvis(raw_revision["reviewers"], "REVIEWER", [])
        ]
        revision.reviewer_removed_ids = [
            self._get_people_id(raw_reviewer) for raw_reviewer in elvis(raw_revision["reviewers"], "REMOVED", [])
        ]
        revision.reviewer_removed_ids = [
            self._get_people_id(raw_reviewer) for raw_reviewer in elvis(raw_revision["reviewers"], "CC", [])
        ]

        # revision.labels
        # revision.more

        return revision.save()

    def get_comments(self, code_review_external_id) -> list[dict]:
        """Fetches all comments for the code review"""

        url = self.base_url + "/changes/" + code_review_external_id + "/comments"

        return self._make_request(url)

    def store_comments(self, raw_comments_obj, code_review_id) -> list[CodeReviewComment]:
        """Stores a comment in the database"""

        comments = []
        insert_list = []

        for file_path, raw_comments in raw_comments_obj.items():
            for raw_comment in raw_comments:
                try:
                    comment = CodeReviewComment.objects.get(external_id=raw_comment["id"])
                except DoesNotExist:
                    comment = CodeReviewComment(external_id=raw_comment["id"])

                comment.code_review_id = code_review_id
                comment.external_id = raw_comment["id"]

                comment.patch_set_number = raw_comment["patch_set"]
                comment.revision_id = self._get_revision_id(code_review_id, raw_comment["patch_set"])

                comment.author_id = self._get_people_id(raw_comment["author"])
                comment.message = raw_comment["message"]
                comment.in_reply_to_id = elvis(raw_comment, "in_reply_to")

                comment.updated_at = parse_date(raw_comment["updated"])

                comment.file_path = file_path
                comment.commit_id = raw_comment["commit_id"]
                comment.line = elvis(raw_comment, "line")

                comment.more = {
                    "change_message_id": raw_comment["change_message_id"],
                    "unresolved": raw_comment["unresolved"],
                }

                if comment.id is not None:
                    comments.append(comment.save())
                else:
                    insert_list.append(comment)

        if len(insert_list) > 0:
            comments.extend(CodeReviewComment.objects.insert(insert_list))

        return comments

    def _store_people(self, raw_people) -> People:
        """Stores a people in the database"""

        try:
            # Try to identify the user by their email. If no email is given try the username.
            if "email" in raw_people:
                saved_people = People.objects.get(email=raw_people["email"], name=raw_people["name"])
            else:
                saved_people = People.objects.get(username=raw_people["username"], name=raw_people["name"])

        except DoesNotExist:
            username = elvis(raw_people, "username", f'{raw_people["name"]}@no_username.gerrit.reviewSHARK')
            email = elvis(raw_people, "email", f"{username}@no_email.gerrit.reviewSHARK")

            people = People(
                username=username,
                email=email,
                name=raw_people["name"],
            )
            saved_people = people.save()

        self.people_id_cache[saved_people.email] = saved_people.id

        return saved_people

    def _make_request(self, url, params=None):
        """Makes a request to Gerrit"""

        tries = 1
        while tries <= 3:
            response = requests.get(url, params)
            self._log.debug("Gerrit request: %s", response.url)

            if response.status_code != 200:
                self._log.error(
                    "Problem with getting data via url %s. Code: %s, Error: %s",
                    url,
                    response.status_code,
                    response.text,
                )

                tries += 1
                time.sleep(2)
            else:
                content = response.content.splitlines()[-1]
                data = json.loads(content)
                return data

        return None

    def _get_revision_id(self, code_review_id, revision_number):
        """Returns the code review revision id"""

        if code_review_id not in self.revision_id_cache:
            self.revision_id_cache[code_review_id] = {}

        if revision_number in self.revision_id_cache[code_review_id]:
            return self.revision_id_cache[code_review_id][revision_number]

        revision = self.revision_id_cache[code_review_id][revision_number] = CodeReviewRevision.objects.get(
            code_review_id=code_review_id, revision_number=revision_number
        )
        self.revision_id_cache[code_review_id][revision_number] = revision.id
        return revision.id

    def _get_people_id(self, raw_people):
        """Returns the people id"""

        if not raw_people:
            return None

        if raw_people["_account_id"] not in self.people_id_cache:
            people = self._store_people(raw_people)
            self.people_id_cache[raw_people["_account_id"]] = people.id

        return self.people_id_cache[raw_people["_account_id"]]
