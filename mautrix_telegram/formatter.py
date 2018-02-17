# -*- coding: future_fstrings -*-
# mautrix-telegram - A Matrix-Telegram puppeting bridge
# Copyright (C) 2018 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from html import escape, unescape
from html.parser import HTMLParser
from collections import deque
import re
import logging

from mautrix_appservice import MatrixRequestError

from telethon.tl.types import *

from . import user as u, puppet as p
from .db import Message as DBMessage

log = logging.getLogger("mau.formatter")

# TEXT LEN EXPLANATION:
# Telegram formatting counts two bytes in an UTF-16 string as one character.
#
# For Telegram -> Matrix formatting, we get the same counting mechanism by encoding the input
# text as UTF-16 Little Endian and doubling all the offsets and lengths given by Telegram. With
# those doubled values, we process the input entities and text. The text is converted back to
# native str format before it's inserted into the output HTML.
#
# For Matrix -> Telegram formatting, do the same input encoding, but divide the length by two
# instead of multiplying when generating the lengths and offsets of Telegram entities.
#
# The endianness doesn't matter, but it has to be specified to avoid the two BOM bits messing
# everything up.
TEMP_ENC = "utf-16-le"


# region Matrix to Telegram


class MatrixParser(HTMLParser):
    mention_regex = re.compile("https://matrix.to/#/(@.+)")

    def __init__(self):
        super().__init__()
        self.text = ""
        self.entities = []
        self._building_entities = {}
        self._list_counter = 0
        self._open_tags = deque()
        self._open_tags_meta = deque()
        self._previous_ended_line = True

    def handle_starttag(self, tag, attrs):
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(0)
        attrs = dict(attrs)
        entity_type = None
        args = {}
        if tag == "strong" or tag == "b":
            entity_type = MessageEntityBold
        elif tag == "em" or tag == "i":
            entity_type = MessageEntityItalic
        elif tag == "code":
            try:
                pre = self._building_entities["pre"]
                try:
                    pre.language = attrs["class"][len("language-"):]
                except KeyError:
                    pass
            except KeyError:
                entity_type = MessageEntityCode
        elif tag == "pre":
            entity_type = MessageEntityPre
            args["language"] = ""
        elif tag == "a":
            try:
                url = attrs["href"]
            except KeyError:
                return
            mention = self.mention_regex.search(url)
            if mention:
                mxid = mention.group(1)
                user = p.Puppet.get_by_mxid(mxid, create=False)
                if not user:
                    user = u.User.get_by_mxid(mxid, create=False)
                    if not user:
                        return
                if user.username:
                    entity_type = MessageEntityMention
                    url = f"@{user.username}"
                else:
                    entity_type = MessageEntityMentionName
                    args["user_id"] = user.tgid
            elif url.startswith("mailto:"):
                url = url[len("mailto:"):]
                entity_type = MessageEntityEmail
            else:
                if self.get_starttag_text() == url:
                    entity_type = MessageEntityUrl
                else:
                    entity_type = MessageEntityTextUrl
                    args["url"] = url
                    url = None
            self._open_tags_meta.popleft()
            self._open_tags_meta.appendleft(url)

        if entity_type and tag not in self._building_entities:
            # See "TEXT LEN EXPLANATION" near start of file
            offset = int(len(self.text.encode(TEMP_ENC)) / 2)
            self._building_entities[tag] = entity_type(offset=offset, length=0, **args)

    def _list_depth(self):
        depth = 0
        for tag in self._open_tags:
            if tag == "ol" or tag == "ul":
                depth += 1
        return depth

    def handle_data(self, text):
        text = unescape(text)
        previous_tag = self._open_tags[0] if len(self._open_tags) > 0 else ""
        list_format_offset = 0
        if previous_tag == "a":
            url = self._open_tags_meta[0]
            if url:
                text = url
        elif len(self._open_tags) > 1 and self._previous_ended_line and previous_tag == "li":
            list_type = self._open_tags[1]
            indent = (self._list_depth() - 1) * 4 * " "
            text = text.strip("\n")
            if len(text) == 0:
                return
            elif list_type == "ul":
                text = f"{indent}* {text}"
                list_format_offset = len(indent) + 2
            elif list_type == "ol":
                n = self._open_tags_meta[1]
                n += 1
                self._open_tags_meta[1] = n
                text = f"{indent}{n}. {text}"
                list_format_offset = len(indent) + 3
        for tag, entity in self._building_entities.items():
            # See "TEXT LEN EXPLANATION" near start of file
            entity.length += int(len(text.strip("\n").encode(TEMP_ENC)) / 2)
            entity.offset += list_format_offset

        if text.endswith("\n"):
            self._previous_ended_line = True
        else:
            self._previous_ended_line = False

        self.text += text

    def handle_endtag(self, tag):
        try:
            self._open_tags.popleft()
            self._open_tags_meta.popleft()
        except IndexError:
            pass
        if (tag == "ul" or tag == "ol") and self.text.endswith("\n"):
            self.text = self.text[:-1]
        entity = self._building_entities.pop(tag, None)
        if entity:
            self.entities.append(entity)


def matrix_to_telegram(html):
    try:
        parser = MatrixParser()
        parser.feed(html)
        return parser.text, parser.entities
    except Exception:
        log.exception("Failed to convert Matrix format:\nhtml=%s", html)


def matrix_reply_to_telegram(content, tg_space, room_id=None):
    try:
        reply = content["m.relates_to"]["m.in_reply_to"]
        room_id = room_id or reply["room_id"]
        event_id = reply["event_id"]
        print(event_id, tg_space, room_id)
        message = DBMessage.query.filter(DBMessage.mxid == event_id,
                                         DBMessage.tg_space == tg_space,
                                         DBMessage.mx_room == room_id).one_or_none()
        if message:
            return message.tgid
    except KeyError:
        pass
    return None


# endregion
# region Telegram to Matrix

def telegram_reply_to_matrix(evt, source):
    if evt.reply_to_msg_id:
        space = (evt.to_id.channel_id
                 if isinstance(evt, Message) and isinstance(evt.to_id, PeerChannel)
                 else source.tgid)
        msg = DBMessage.query.get((evt.reply_to_msg_id, space))
        if msg:
            return {
                "m.in_reply_to": {
                    "event_id": msg.mxid,
                    "room_id": msg.mx_room,
                }
            }
    return {}


async def telegram_event_to_matrix(evt, source, native_replies=False, message_link_in_reply=False,
                                   main_intent=None, reply_text="Reply"):
    text = evt.message
    html = telegram_to_matrix(evt.message, evt.entities) if evt.entities else None
    relates_to = {}

    if evt.fwd_from:
        if not html:
            html = escape(text)
        from_id = evt.fwd_from.from_id
        user = u.User.get_by_tgid(from_id)
        if user:
            fwd_from = f"<a href='https://matrix.to/#/{user.mxid}'>{user.mxid}</a>"
        else:
            puppet = p.Puppet.get(from_id, create=False)
            if puppet and puppet.displayname:
                fwd_from = f"<a href='https://matrix.to/#/{puppet.mxid}'>{puppet.displayname}</a>"
            else:
                user = await source.client.get_entity(from_id)
                if user:
                    fwd_from = p.Puppet.get_displayname(user, format=False)
                else:
                    fwd_from = None
        if not fwd_from:
            fwd_from = "Unknown user"
        html = (f"Forwarded message from <b>{fwd_from}</b><br/>"
                + f"<blockquote>{html}</blockquote>")

    if evt.reply_to_msg_id:
        space = (evt.to_id.channel_id
                 if isinstance(evt, Message) and isinstance(evt.to_id, PeerChannel)
                 else source.tgid)
        msg = DBMessage.query.get((evt.reply_to_msg_id, space))
        if msg:
            if native_replies:
                relates_to["m.in_reply_to"] = {
                    "event_id": msg.mxid,
                    "room_id": msg.mx_room,
                }
                if reply_text == "Edit":
                    html = "<u>Edit:</u> " + (html or escape(text))
            else:
                try:
                    event = await main_intent.get_event(msg.mx_room, msg.mxid)
                    content = event["content"]
                    body = (content["formatted_body"]
                            if "formatted_body" in content
                            else content["body"])
                    sender = event['sender']
                    puppet = p.Puppet.get_by_mxid(sender, create=False)
                    displayname = puppet.displayname if puppet else sender
                    reply_to_user = f"<a href='https://matrix.to/#/{sender}'>{displayname}</a>"
                    reply_to_msg = (("<a href='https://matrix.to/#/"
                                     + f"{msg.mx_room}/{msg.mxid}'>{reply_text}</a>")
                                    if message_link_in_reply else "Reply")
                    quote = f"{reply_to_msg} to {reply_to_user}<blockquote>{body}</blockquote>"
                except (ValueError, KeyError, MatrixRequestError):
                    quote = "{reply_text} to unknown user <em>(Failed to fetch message)</em>:<br/>"
                if html:
                    html = quote + html
                else:
                    html = quote + escape(text)

    if isinstance(evt, Message) and evt.post and evt.post_author:
        if not html:
            html = escape(text)
        text += f"\n- {evt.post_author}"
        html += f"<br/><i>- <u>{evt.post_author}</u></i>"

    if html:
        html = html.replace("\n", "<br/>")

    return text, html, relates_to


def telegram_to_matrix(text, entities):
    try:
        return _telegram_to_matrix(text, entities)
    except Exception:
        log.exception("Failed to convert Telegram format:\n"
                      "message=%s\n"
                      "entities=%s",
                      text, entities)


def _telegram_to_matrix(text, entities):
    if not entities:
        return text
    # See "TEXT LEN EXPLANATION" near start of file
    text = text.encode(TEMP_ENC)
    html = []
    last_offset = 0
    for entity in entities:
        entity.offset *= 2
        entity.length *= 2
        if entity.offset > last_offset:
            html.append(escape(text[last_offset:entity.offset].decode(TEMP_ENC)))
        elif entity.offset < last_offset:
            continue

        skip_entity = False
        entity_text = escape(text[entity.offset:entity.offset + entity.length].decode(TEMP_ENC))
        entity_type = type(entity)

        if entity_type == MessageEntityBold:
            html.append(f"<strong>{entity_text}</strong>")
        elif entity_type == MessageEntityItalic:
            html.append(f"<em>{entity_text}</em>")
        elif entity_type == MessageEntityCode:
            html.append(f"<code>{entity_text}</code>")
        elif entity_type == MessageEntityPre:
            if entity.language:
                html.append("<pre>"
                            + f"<code class='language-{entity.language}'>{entity_text}</code>"
                            + "</pre>")
            else:
                html.append(f"<pre><code>{entity_text}</code></pre>")
        elif entity_type == MessageEntityMention:
            username = entity_text[1:]

            user = u.User.find_by_username(username)
            if user:
                mxid = user.mxid
            else:
                puppet = p.Puppet.find_by_username(username)
                mxid = puppet.mxid if puppet else None
            if mxid:
                html.append(f"<a href='https://matrix.to/#/{mxid}'>{entity_text}</a>")
            else:
                skip_entity = True
        elif entity_type == MessageEntityMentionName:
            user = u.User.get_by_tgid(entity.user_id)
            if user:
                mxid = user.mxid
            else:
                puppet = p.Puppet.get(entity.user_id, create=False)
                mxid = puppet.mxid if puppet else None
            if mxid:
                html.append(f"<a href='https://matrix.to/#/{mxid}'>{entity_text}</a>")
            else:
                skip_entity = True
        elif entity_type == MessageEntityEmail:
            html.append(f"<a href='mailto:{entity_text}'>{entity_text}</a>")
        elif entity_type in {MessageEntityTextUrl, MessageEntityUrl}:
            url = escape(entity.url) if entity_type == MessageEntityTextUrl else entity_text
            if not url.startswith(("https://", "http://", "ftp://", "magnet://")):
                url = "http://" + url
            html.append(f"<a href='{url}'>{entity_text}</a>")
        elif entity_type == MessageEntityBotCommand:
            html.append(f"<font color='blue'>!{entity_text[1:]}")
        elif entity_type == MessageEntityHashtag:
            html.append(f"<font color='blue'>{entity_text}</font>")
        else:
            skip_entity = True
        last_offset = entity.offset + (0 if skip_entity else entity.length)
    html.append(text[last_offset:].decode(TEMP_ENC))

    return "".join(html)

# endregion
