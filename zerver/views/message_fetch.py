from collections.abc import Iterable
from typing import Annotated

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _
from pydantic import Json, NonNegativeInt
from sqlalchemy.sql import column, func
from sqlalchemy.types import Integer, Text

from zerver.context_processors import get_valid_realm_from_request
from zerver.lib.exceptions import (
    IncompatibleParametersError,
    JsonableError,
    MissingAuthenticationError,
)
from zerver.lib.message import get_first_visible_message_id, messages_for_ids
from zerver.lib.narrow import (
    NarrowParameter,
    add_narrow_conditions,
    clean_narrow_for_message_fetch,
    fetch_messages,
    get_base_query_for_search,
    is_spectator_compatible,
    is_web_public_narrow,
    parse_anchor_value,
    update_narrow_terms_containing_empty_topic_fallback_name,
)
from zerver.lib.request import RequestNotes
from zerver.lib.response import json_success
from zerver.lib.sqlalchemy_utils import get_sqlalchemy_connection
from zerver.lib.topic import MATCH_TOPIC
from zerver.lib.topic_sqlalchemy import topic_column_sa
from zerver.lib.typed_endpoint import ApiParamConfig, typed_endpoint
from zerver.models import UserMessage, UserProfile

MAX_MESSAGES_PER_FETCH = 5000


def highlight_string(text: str, locs: Iterable[tuple[int, int]]) -> str:
    highlight_start = '<span class="highlight">'
    highlight_stop = "</span>"
    pos = 0
    result = ""
    in_tag = False

    for loc in locs:
        (offset, length) = loc

        prefix_start = pos
        prefix_end = offset
        match_start = offset
        match_end = offset + length

        prefix = text[prefix_start:prefix_end]
        match = text[match_start:match_end]

        for character in prefix + match:
            if character == "<":
                in_tag = True
            elif character == ">":
                in_tag = False
        if in_tag:
            result += prefix
            result += match
        else:
            result += prefix
            result += highlight_start
            result += match
            result += highlight_stop
        pos = match_end

    result += text[pos:]
    return result


def get_search_fields(
    rendered_content: str,
    escaped_topic_name: str,
    content_matches: Iterable[tuple[int, int]],
    topic_matches: Iterable[tuple[int, int]],
) -> dict[str, str]:
    return {
        "match_content": highlight_string(rendered_content, content_matches),
        MATCH_TOPIC: highlight_string(escaped_topic_name, topic_matches),
    }


def clean_narrow_for_web_public_api(
    narrow: list[NarrowParameter] | None,
) -> list[NarrowParameter] | None:
    if narrow is None:
        return None

    # Remove {'operator': 'in', 'operand': 'home', 'negated': False} from narrow.
    # This is to allow spectators to access all messages. The narrow should still pass
    # is_web_public_narrow check after this change.
    return [
        term
        for term in narrow
        if not (term.operator == "in" and term.operand == "home" and not term.negated)
    ]


@typed_endpoint
def get_messages_backend(
    request: HttpRequest,
    maybe_user_profile: UserProfile | AnonymousUser,
    *,
    allow_empty_topic_name: Json[bool] = False,
    anchor_val: Annotated[str | None, ApiParamConfig("anchor")] = None,
    apply_markdown: Json[bool] = True,
    client_gravatar: Json[bool] = True,
    client_requested_message_ids: Annotated[
        Json[list[NonNegativeInt] | None], ApiParamConfig("message_ids")
    ] = None,
    include_anchor: Json[bool] = True,
    narrow: Json[list[NarrowParameter] | None] = None,
    num_after: Json[NonNegativeInt] = 0,
    num_before: Json[NonNegativeInt] = 0,
    use_first_unread_anchor_val: Annotated[
        Json[bool], ApiParamConfig("use_first_unread_anchor")
    ] = False,
) -> HttpResponse:
    # User has to either provide message_ids or both num_before and num_after.
    if (
        num_before or num_after or anchor_val is not None or use_first_unread_anchor_val
    ) and client_requested_message_ids is not None:
        raise IncompatibleParametersError(
            [
                "num_before",
                "num_after",
                "anchor",
                "message_ids",
                "include_anchor",
                "use_first_unread_anchor",
            ]
        )
    elif client_requested_message_ids is not None:
        include_anchor = False

    anchor = None
    if client_requested_message_ids is None:
        anchor = parse_anchor_value(anchor_val, use_first_unread_anchor_val)

    realm = get_valid_realm_from_request(request)
    narrow = clean_narrow_for_message_fetch(narrow, realm, maybe_user_profile)

    num_of_messages_requested = num_before + num_after
    if client_requested_message_ids is not None:
        num_of_messages_requested = len(client_requested_message_ids)

    if num_of_messages_requested > MAX_MESSAGES_PER_FETCH:
        raise JsonableError(
            _("Too many messages requested (maximum {max_messages}).").format(
                max_messages=MAX_MESSAGES_PER_FETCH,
            )
        )
    if num_before > 0 and num_after > 0 and not include_anchor:
        raise JsonableError(_("The anchor can only be excluded at an end of the range"))

    if not maybe_user_profile.is_authenticated:
        # If user is not authenticated, clients must include
        # `streams:web-public` in their narrow query to indicate this
        # is a web-public query.  This helps differentiate between
        # cases of web-public queries (where we should return the
        # web-public results only) and clients with buggy
        # authentication code (where we should return an auth error).
        #
        # GetOldMessagesTest.test_unauthenticated_* tests ensure
        # that we are not leaking any secure data (direct messages and
        # non-web-public stream messages) via this path.
        if not realm.allow_web_public_streams_access():
            raise MissingAuthenticationError
        narrow = clean_narrow_for_web_public_api(narrow)
        if not is_web_public_narrow(narrow):
            raise MissingAuthenticationError
        assert narrow is not None
        if not is_spectator_compatible(narrow):
            raise MissingAuthenticationError

        # We use None to indicate unauthenticated requests as it's more
        # readable than using AnonymousUser, and the lack of Django
        # stubs means that mypy can't check AnonymousUser well.
        user_profile: UserProfile | None = None
        is_web_public_query = True
    else:
        assert isinstance(maybe_user_profile, UserProfile)
        user_profile = maybe_user_profile
        assert user_profile is not None
        is_web_public_query = False

    assert realm is not None

    if is_web_public_query:
        # client_gravatar here is just the user-requested value. "finalize_payload" function
        # is responsible for sending avatar_url based on each individual sender's
        # email_address_visibility setting.
        client_gravatar = False

    if narrow is not None:
        # Add some metadata to our logging data for narrows
        verbose_operators = []
        for term in narrow:
            if term.operator == "is":
                verbose_operators.append("is:" + term.operand)
            else:
                verbose_operators.append(term.operator)
        log_data = RequestNotes.get_notes(request).log_data
        assert log_data is not None
        log_data["extra"] = "[{}]".format(",".join(verbose_operators))

    with transaction.atomic(durable=True):
        # We're about to perform a search, and then get results from
        # it; this is done across multiple queries.  To prevent race
        # conditions, we want the messages returned to be consistent
        # with the version of the messages that was searched, to
        # prevent changes which happened between them from leaking to
        # clients who should not be able to see the new values, and
        # when messages are deleted in between.  We set up
        # repeatable-read isolation for this transaction, so that we
        # prevent both phantom reads and non-repeatable reads.
        #
        # In a read-only repeatable-read transaction, it is not
        # possible to encounter deadlocks or need retries due to
        # serialization errors.
        #
        # You can only set the isolation level before any queries in
        # the transaction, meaning it must be the top-most
        # transaction, which durable=True establishes.  Except in
        # tests, where durable=True is a lie, because there is an
        # outer transaction for each test.  We thus skip this command
        # in tests, since it would fail.
        if not settings.TEST_SUITE:  # nocoverage
            cursor = connection.cursor()
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

        query_info = fetch_messages(
            narrow=narrow,
            user_profile=user_profile,
            realm=realm,
            is_web_public_query=is_web_public_query,
            anchor=anchor,
            include_anchor=include_anchor,
            num_before=num_before,
            num_after=num_after,
            client_requested_message_ids=client_requested_message_ids,
        )

        anchor = query_info.anchor
        include_history = query_info.include_history
        is_search = query_info.is_search
        rows = query_info.rows

        # The following is a little messy, but ensures that the code paths
        # are similar regardless of the value of include_history.  The
        # 'user_messages' dictionary maps each message to the user's
        # UserMessage object for that message, which we will attach to the
        # rendered message dict before returning it.  We attempt to
        # bulk-fetch rendered message dicts from remote cache using the
        # 'messages' list.
        result_message_ids: list[int] = []
        user_message_flags: dict[int, list[str]] = {}
        if is_web_public_query:
            # For spectators, we treat all historical messages as read.
            for row in rows:
                message_id = row[0]
                result_message_ids.append(message_id)
                user_message_flags[message_id] = ["read"]
        elif include_history:
            assert user_profile is not None
            result_message_ids = [row[0] for row in rows]

            # TODO: This could be done with an outer join instead of two queries
            um_rows = UserMessage.objects.filter(
                user_profile=user_profile, message_id__in=result_message_ids
            )
            user_message_flags = {um.message_id: um.flags_list() for um in um_rows}

            for message_id in result_message_ids:
                if message_id not in user_message_flags:
                    user_message_flags[message_id] = ["read", "historical"]
        else:
            for row in rows:
                message_id = row[0]
                flags = row[1]
                user_message_flags[message_id] = UserMessage.flags_list_for_flags(flags)
                result_message_ids.append(message_id)

        search_fields: dict[int, dict[str, str]] = {}
        if is_search:
            for row in rows:
                message_id = row[0]
                (escaped_topic_name, rendered_content, content_matches, topic_matches) = row[-4:]
                search_fields[message_id] = get_search_fields(
                    rendered_content, escaped_topic_name, content_matches, topic_matches
                )

        message_list = messages_for_ids(
            message_ids=result_message_ids,
            user_message_flags=user_message_flags,
            search_fields=search_fields,
            apply_markdown=apply_markdown,
            client_gravatar=client_gravatar,
            allow_empty_topic_name=allow_empty_topic_name,
            message_edit_history_visibility_policy=realm.message_edit_history_visibility_policy,
            user_profile=user_profile,
            realm=realm,
        )

    if client_requested_message_ids is not None:
        ret = dict(
            messages=message_list,
            result="success",
            msg="",
            history_limited=query_info.history_limited,
            found_anchor=False,
            found_oldest=False,
            found_newest=False,
        )
    else:
        ret = dict(
            messages=message_list,
            result="success",
            msg="",
            found_anchor=query_info.found_anchor,
            found_oldest=query_info.found_oldest,
            found_newest=query_info.found_newest,
            history_limited=query_info.history_limited,
            anchor=anchor,
        )

    return json_success(request, data=ret)


@typed_endpoint
def messages_in_narrow_backend(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    msg_ids: Json[list[int]],
    narrow: Json[list[NarrowParameter]],
) -> HttpResponse:
    first_visible_message_id = get_first_visible_message_id(user_profile.realm)
    msg_ids = [message_id for message_id in msg_ids if message_id >= first_visible_message_id]
    # This query is limited to messages the user has access to because they
    # actually received them, as reflected in `zerver_usermessage`.
    query, inner_msg_id_col = get_base_query_for_search(
        user_profile.realm_id, user_profile, need_user_message=True
    )
    query = query.where(column("message_id", Integer).in_(msg_ids))

    updated_narrow = update_narrow_terms_containing_empty_topic_fallback_name(narrow)
    query, is_search, _is_dm_narrow = add_narrow_conditions(
        user_profile=user_profile,
        inner_msg_id_col=inner_msg_id_col,
        query=query,
        narrow=updated_narrow,
        is_web_public_query=False,
        realm=user_profile.realm,
    )

    if not is_search:
        # `add_narrow_conditions` adds the following columns only if narrow has search operands.
        query = query.add_columns(
            func.escape_html(topic_column_sa(), type_=Text).label("escaped_topic_name"),
            column("rendered_content", Text),
        )

    search_fields = {}
    with get_sqlalchemy_connection() as sa_conn:
        for row in sa_conn.execute(query).mappings():
            message_id = row["message_id"]
            escaped_topic_name: str = row["escaped_topic_name"]
            rendered_content: str = row["rendered_content"]
            content_matches = row.get("content_matches", [])
            topic_matches = row.get("topic_matches", [])
            search_fields[str(message_id)] = get_search_fields(
                rendered_content,
                escaped_topic_name,
                content_matches,
                topic_matches,
            )

    return json_success(request, data={"messages": search_fields})
