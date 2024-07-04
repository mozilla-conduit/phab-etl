#!/bin/env python3

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sqlalchemy
from sqlalchemy import desc, or_
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import Session

DB_URL = os.environ.get("PHAB_URL", "127.0.0.1")
DB_NAMESPACE = os.environ.get("PHAB_NAMESPACE", "bitnami_phabricator")
DB_PORT = os.environ.get("PHAB_PORT", "3307")
DB_USER = os.environ.get("PHAB_USER", "root")
DB_TOKEN = os.environ["PHAB_TOKEN"]


def create_engine(table_suffix):
    return sqlalchemy.create_engine(
        f"mysql+mysqldb://{DB_USER}:{DB_TOKEN}@{DB_URL}:{DB_PORT}/{DB_NAMESPACE}_{table_suffix}"
    )


def create_engines():
    return {
        "user": create_engine("user"),
        "project": create_engine("project"),
        "repository": create_engine("repository"),
        "differential": create_engine("differential"),
    }


def prepare_bases(engines):
    Base = automap_base()
    bases = {}
    for key, engine in engines.items():
        Base.prepare(engine)
        bases[key] = Base
    return bases


engines = create_engines()
bases = prepare_bases(engines)


@dataclass
class UserDb:
    User = bases["user"].classes.user


@dataclass
class ProjectDb:
    Project = bases["project"].classes.project


@dataclass
class RepoDb:
    Repository = bases["repository"].classes.repository_uri


@dataclass
class DiffDb:
    Revision = bases["differential"].classes.differential_revision
    Differential = bases["differential"].classes.differential_diff
    Changeset = bases["differential"].classes.differential_changeset
    Transaction = bases["differential"].classes.differential_transaction
    TransactionComment = bases["differential"].classes.differential_transaction_comment
    Reviewer = bases["differential"].classes.differential_reviewer
    Edges = bases["differential"].classes.edge


def get_last_review_id(revision_phid, session_diff):
    last_review = (
        session_diff.query(DiffDb.Reviewer)
        .filter_by(revisionPHID=revision_phid)
        .order_by(desc("dateModified"))
        .first()
    )
    return last_review.id if last_review else None


def get_target_repository(repository_phid, session_repo):
    repository = (
        session_repo.query(RepoDb.Repository)
        .filter_by(repositoryPHID=repository_phid)
        .first()
    )
    return repository.uri


def get_stack_size(revision, revisions, session_diff):
    stack = set()
    neighbors = {revision.phid}
    bug_id = revision.title.split("-")[0]
    while neighbors:
        query_result = (
            session_diff.query(DiffDb.Edges)
            .filter(
                or_(DiffDb.Edges.src.in_(neighbors), DiffDb.Edges.dst.in_(neighbors)),
                DiffDb.Edges.type.in_([5, 6]),
            )
            .all()
        )
        revlist = []
        for edge in query_result:
            for rev_src in revisions.filter_by(phid=edge.src):
                if rev_src.title.split("-")[0] == bug_id:
                    revlist.append(rev_src.phid)
            for rev_dst in revisions.filter_by(phid=edge.dst):
                if rev_dst.title.split("-")[0] == bug_id:
                    revlist.append(rev_dst.phid)
        stack.update(neighbors)
        neighbors = set(revlist) - stack
    return len(stack)


def get_user_name(authorPHID, session_users):
    user = session_users.query(UserDb.User).filter_by(phid=authorPHID).one()
    return user.userName


def get_review_requests(revision_phid, session_diff, session_projects, session_users):
    review_requests = {}
    for review in session_diff.query(DiffDb.Reviewer).filter_by(
        revisionPHID=revision_phid
    ):
        is_reviewer_group = review.reviewerPHID.startswith(b"PHID-PROJ-")
        if is_reviewer_group:
            reviewer = (
                session_projects.query(ProjectDb.Project)
                .filter_by(phid=review.reviewerPHID)
                .one()
            )
            reviewer_name = reviewer.name
        else:
            reviewer = (
                session_users.query(UserDb.User)
                .filter_by(phid=review.reviewerPHID)
                .one()
            )
            reviewer_name = reviewer.userName
        review_requests[f"review-{review.id}"] = {
            "reviewer": reviewer_name,
            "is group": is_reviewer_group,
            "creation timestamp": review.dateCreated,
            "review timestamp": review.dateModified,
            "status": review.reviewerStatus,
        }
    return review_requests


def get_diffs(revision, session_diff, session_users, session_projects):
    diffs = {}
    for diff in session_diff.query(DiffDb.Differential).filter_by(
        revisionID=revision.id
    ):
        if diff.authorPHID == b"PHID-APPS-PhabricatorDiffusionApplication":
            # ignore diffs that were created as a result of the commit landing.
            continue
        if diff.authorPHID.startswith(b"PHID-RIDT-"):
            # ignore diffs that were created with repository identity
            continue
        diffs[f"diff-{diff.id}"] = {
            "submission time (dateCreated)": diff.dateCreated,
            "author (userName)": get_user_name(diff.authorPHID, session_users),
            "changesets": get_changesets(diff, session_diff, session_users),
            "review requests": get_review_requests(
                revision.phid, session_diff, session_projects, session_users
            ),
        }
    return diffs


def get_changesets(diff, session_diff, session_users):
    changesets = {}
    for changeset in session_diff.query(DiffDb.Changeset).filter_by(diffID=diff.id):
        changeset_id = f"changeset-{changeset.id}"
        changesets[changeset_id] = {
            "lines added": changeset.addLines,
            "lines removed": changeset.delLines,
            "comments": get_changeset_comments(changeset, session_diff, session_users),
        }
    return changesets


def get_changeset_comments(changeset, session_diff, session_users):
    comments = {}
    for comment in session_diff.query(DiffDb.TransactionComment).filter_by(
        changesetID=changeset.id
    ):
        comment_id = f"comment-{comment.id}"
        user = session_users.query(UserDb.User).filter_by(phid=comment.authorPHID).one()
        att = json.loads(comment.attributes)
        is_suggestion = (
            "inline.state.initial" in att
            and att["inline.state.initial"].get("hassuggestion") == "true"
        )
        comments[comment_id] = {
            "author": user.userName,
            "timestamp (dateCreated)": comment.dateCreated,
            "character count": len(comment.content),
            "is_suggestion": is_suggestion,
        }
    return comments


def get_comments(revision_phid, session_diff, session_users):
    comments = {}
    for transaction in session_diff.query(DiffDb.Transaction).filter_by(
        objectPHID=revision_phid,
        transactionType="core:comment",
    ):
        comment = (
            session_diff.query(DiffDb.TransactionComment)
            .filter_by(phid=transaction.commentPHID)
            .one()
        )
        user = session_users.query(UserDb.User).filter_by(phid=comment.authorPHID).one()
        comment_id = f"comment-{comment.id}"
        comments[comment_id] = {
            "author": user.userName,
            "timestamp (dateCreated)": comment.dateCreated,
            "character count": len(comment.content),
        }
    return comments


def export_to_json(output):
    now = datetime.now().strftime("%Y%m%d")
    Path(f"revisions_{now}.json").write_text(json.dumps(output, indent=2))


def process():
    session_users = Session(engines["user"])
    session_projects = Session(engines["project"])
    session_repo = Session(engines["repository"])
    session_diff = Session(engines["differential"])
    output = {}
    revisions = session_diff.query(DiffDb.Revision)
    for revision in revisions:
        output[f"D{revision.id}"] = {
            "first submission timestamp (dateCreated)": revision.dateCreated,
            "last review id": get_last_review_id(revision.phid, session_diff),
            "current status": revision.status,
            "target repository": get_target_repository(
                revision.repositoryPHID, session_repo
            ),
            "stack size": get_stack_size(revision, revisions, session_diff),
            "diffs": get_diffs(
                revision,
                session_diff,
                session_users,
                session_projects,
            ),
            "comments": get_comments(revision.phid, session_diff, session_users),
        }
    export_to_json(output)


if __name__ == "__main__":
    process()
