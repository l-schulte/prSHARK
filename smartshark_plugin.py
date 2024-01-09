#!/usr/bin/env python

"""Plugin for execution with serverSHARK."""

import sys
import logging
import timeit
import datetime

from mongoengine import connect

from pycoshark.utils import get_base_argparser, create_mongodb_uri_string
from pycoshark.mongomodels import Project, CodeReviewSystem

from reviewSHARK.config import Config

log = logging.getLogger("reviewSHARK")
log.setLevel(logging.INFO)
i = logging.StreamHandler(sys.stdout)
e = logging.StreamHandler(sys.stderr)

i.setLevel(logging.DEBUG)
e.setLevel(logging.ERROR)

log.addHandler(i)
log.addHandler(e)


def main(args):
    if args.log_level:
        log.setLevel(args.log_level)

    # timing
    start = timeit.default_timer()

    cfg = Config(args)

    # create connection
    uri = create_mongodb_uri_string(
        args.db_user, args.db_password, args.db_hostname, args.db_port, args.db_authentication, args.ssl
    )
    connect(args.db_database, host=uri)

    # Get the project for which issue data is collected
    try:
        project = Project.objects.get(name=cfg.project_name)
    except Project.DoesNotExist:
        log.error("Project %s not found!", cfg.project_name)
        sys.exit(1)

    # Create pull request system if not already there
    try:
        codereview_system = CodeReviewSystem.objects.get(url=cfg.tracking_url, project_id=project.id)
    except CodeReviewSystem.DoesNotExist:
        codereview_system = CodeReviewSystem(project_id=project.id, url=cfg.tracking_url)
    codereview_system.last_updated = datetime.datetime.now()
    codereview_system.save()

    # now we do the actual work, we are lazy for now and to not use a more dynamic import of backends
    if args.backend == "gerrit":
        from reviewSHARK.backends.gerrit import Gerrit

        gerrit = Gerrit(cfg, project, codereview_system)
        gerrit.run()
    else:
        log.error("Backend %s not implemented", args.backend)
        sys.exit(1)

    end = timeit.default_timer() - start
    log.info("Finished data extraction in {:.5f}s".format(end))


if __name__ == "__main__":
    available_backends = "gerrit"
    parser = get_base_argparser("Collects information from pull requests", "0.0.0")
    parser.add_argument("-i", "--prurl", help="URL to the pull request system.", required=True)
    parser.add_argument("-pn", "--project-name", help="Name of the project.", required=True)
    parser.add_argument(
        "-apn", "--alternate-project-name", help="Alternate name of the project for browsing the REST API"
    )
    parser.add_argument(
        "-b", "--backend", help="Backend to use for the parsing", default="gerrit", choices=available_backends
    )
    parser.add_argument("-t", "--token", help="Token for accessing.", default=None)

    parser.add_argument("-PH", "--proxy-host", help="Proxy hostname or IP address.", default=None)
    parser.add_argument("-PP", "--proxy-port", help="Port of the proxy to use.", default=None)
    parser.add_argument("-Pp", "--proxy-password", help="Password to use the proxy (HTTP Basic Auth)", default=None)
    parser.add_argument("-PU", "--proxy-user", help="Username to use the proxy (HTTP Basic Auth)", default=None)
    parser.add_argument("-iU", "--issue-user", help="Username to use the pull request system", default=None)
    parser.add_argument("-iP", "--issue-password", help="Password to use the pull request system", default=None)

    parser.add_argument("--link", default=True)

    parser.add_argument(
        "-ll",
        "--log-level",
        help="Log level for stdout (DEBUG, INFO), default INFO",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    main(parser.parse_args())
