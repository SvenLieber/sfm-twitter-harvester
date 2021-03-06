#!/usr/bin/env python3

from __future__ import absolute_import
import logging
import re
import json
import requests

from twarc import Twarc
from sfmutils.harvester import BaseHarvester, Msg
from twitter_stream_warc_iter import TwitterStreamWarcIter
from twitter_rest_warc_iter import TwitterRestWarcIter

log = logging.getLogger(__name__)

QUEUE = "twitter_rest_harvester"
SEARCH_ROUTING_KEY = "harvest.start.twitter.twitter_search"
TIMELINE_ROUTING_KEY = "harvest.start.twitter.twitter_user_timeline"

status_re = re.compile("^https://twitter.com/.+/status/\d+$")


class TwitterHarvester(BaseHarvester):
    def __init__(self, working_path, stream_restart_interval_secs=30 * 60, mq_config=None, debug=False,
                 connection_errors=5, http_errors=5, debug_warcprox=False, tries=3):
        BaseHarvester.__init__(self, working_path, mq_config=mq_config,
                               stream_restart_interval_secs=stream_restart_interval_secs,
                               debug=debug, debug_warcprox=debug_warcprox, tries=tries)
        self.twarc = None
        self.connection_errors = connection_errors
        self.http_errors = http_errors

    def harvest_seeds(self):
        # Create a twarc
        self._create_twarc()

        # Dispatch message based on type.
        harvest_type = self.message.get("type")
        log.debug("Harvest type is %s", harvest_type)
        if harvest_type == "twitter_search":
            self.search()
        elif harvest_type == "twitter_filter":
            self.filter()
        elif harvest_type == "twitter_sample":
            self.sample()
        elif harvest_type == "twitter_user_timeline":
            self.user_timeline()
        else:
            raise KeyError

    def _create_twarc(self):
        self.twarc = Twarc(self.message["credentials"]["consumer_key"],
                           self.message["credentials"]["consumer_secret"],
                           self.message["credentials"]["access_token"],
                           self.message["credentials"]["access_token_secret"],
                           http_errors=self.http_errors,
                           connection_errors=self.connection_errors,
                           tweet_mode="extended")

    def search(self):
        assert len(self.message.get("seeds", [])) == 1

        incremental = self.message.get("options", {}).get("incremental", False)

        since_id = self.state_store.get_state(__name__,
                                              u"{}.since_id".format(self._search_id())) if incremental else None

        query, geocode = self._search_parameters()
        self._harvest_tweets(self.twarc.search(query, geocode=geocode, since_id=since_id))

    def _search_parameters(self):
        if type(self.message["seeds"][0]["token"]) is dict:
            query = self.message["seeds"][0]["token"].get("query")
            geocode = self.message["seeds"][0]["token"].get("geocode")
        else:
            query = self.message["seeds"][0]["token"]
            geocode = None
        return query, geocode

    def _search_id(self):
        query, geocode = self._search_parameters()
        if query and not geocode:
            return query
        if geocode and not query:
            return geocode
        return ":".join([query, geocode])

    def filter(self):
        assert len(self.message.get("seeds", [])) == 1

        track = self.message["seeds"][0]["token"].get("track")
        follow = self.message["seeds"][0]["token"].get("follow")
        locations = self.message["seeds"][0]["token"].get("locations")
        language = self.message["seeds"][0]["token"].get("language")

        self._harvest_tweets(
            self.twarc.filter(track=track, follow=follow, locations=locations, lang=language, event=self.stop_harvest_seeds_event))

    def sample(self):
        self._harvest_tweets(self.twarc.sample(self.stop_harvest_seeds_event))

    def user_timeline(self):
        incremental = self.message.get("options", {}).get("incremental", False)

        for seed in self.message.get("seeds", []):
            seed_id = seed["id"]
            screen_name = seed.get("token")
            user_id = seed.get("uid")
            log.debug("Processing seed (%s) with screen name %s and user id %s", seed_id, screen_name, user_id)
            assert screen_name or user_id

            # If there is not a user_id, look it up.
            if screen_name and not user_id:
                result, user = self._lookup_user(screen_name, "screen_name")
                if result == "OK":
                    user_id = user["id_str"]
                    self.result.uids[seed_id] = user_id
                else:
                    msg = u"User id not found for {} because account is {}".format(screen_name,
                                                                                   self._result_to_reason(result))
                    log.exception(msg)
                    self.result.warnings.append(Msg("token_{}".format(result), msg, seed_id=seed_id))
            # Otherwise, get the current screen_name
            else:
                result, user = self._lookup_user(user_id, "user_id")
                if result == "OK":
                    new_screen_name = user["screen_name"]
                    if new_screen_name and new_screen_name != screen_name:
                        self.result.token_updates[seed_id] = new_screen_name
                else:
                    msg = u"User {} (User ID: {}) not found because account is {}".format(screen_name, user_id,
                                                                                          self._result_to_reason(
                                                                                              result))
                    log.exception(msg)
                    self.result.warnings.append(Msg("uid_{}".format(result), msg, seed_id=seed_id))
                    user_id = None

            if user_id:
                # Get since_id from state_store
                since_id = self.state_store.get_state(__name__,
                                                      "timeline.{}.since_id".format(
                                                          user_id)) if incremental else None

                self._harvest_tweets(self.twarc.timeline(user_id=user_id, since_id=since_id))

    def _lookup_user(self, id, id_type):
        url = "https://api.twitter.com/1.1/users/show.json"
        params = {id_type: id}

        # USER_DELETED: 404 and {"errors": [{"code": 50, "message": "User not found."}]}
        # USER_PROTECTED: 200 and user object with "protected": true
        # USER_SUSPENDED: 403 and {"errors":[{"code":63,"message":"User has been suspended."}]}
        result = "OK"
        user = None
        try:
            resp = self.twarc.get(url, params=params, allow_404=True)
            user = resp.json()
            if user['protected']:
                result = "unauthorized"
        except requests.exceptions.HTTPError as e:
            try:
                resp_json = e.response.json()
            except json.decoder.JSONDecodeError:
                raise e
            if e.response.status_code == 404 and self._has_error_code(resp_json, 50):
                result = "not_found"
            elif e.response.status_code == 403 and self._has_error_code(resp_json, 63):
                result = "suspended"
            else:
                raise e
        return result, user

    @staticmethod
    def _has_error_code(resp, code):
        if isinstance(code, int):
            code = (code,)
        for error in resp['errors']:
            if error['code'] in code:
                return True
        return False

    @staticmethod
    def _result_to_reason(result):
        if result == "unauthorized":
            return "protected"
        elif result == "suspended":
            return "suspended"
        return "not found or deleted"

    def _harvest_tweets(self, tweets):
        # max_tweet_id = None
        for count, tweet in enumerate(tweets):
            if not count % 100:
                log.debug("Harvested %s tweets", count)
            self.result.harvest_counter["tweets"] += 1
            if self.stop_harvest_seeds_event.is_set():
                log.debug("Stopping since stop event set.")
                break

    def process_warc(self, warc_filepath):
        # Dispatch message based on type.
        harvest_type = self.message.get("type")
        log.debug("Harvest type is %s", harvest_type)
        if harvest_type == "twitter_search":
            self.process_search_warc(warc_filepath)
        elif harvest_type == "twitter_filter":
            self._process_tweets(TwitterStreamWarcIter(warc_filepath))
        elif harvest_type == "twitter_sample":
            self._process_tweets(TwitterStreamWarcIter(warc_filepath))
        elif harvest_type == "twitter_user_timeline":
            self.process_user_timeline_warc(warc_filepath)
        else:
            raise KeyError

    def process_search_warc(self, warc_filepath):
        incremental = self.message.get("options", {}).get("incremental", False)

        since_id = self.state_store.get_state(__name__,
                                              u"{}.since_id".format(self._search_id())) if incremental else None

        max_tweet_id = self._process_tweets(TwitterRestWarcIter(warc_filepath))

        # Update state store
        if incremental and (max_tweet_id or 0) > (since_id or 0):
            self.state_store.set_state(__name__, u"{}.since_id".format(self._search_id()), max_tweet_id)

    def process_user_timeline_warc(self, warc_filepath):
        incremental = self.message.get("options", {}).get("incremental", False)

        for count, status in enumerate(TwitterRestWarcIter(warc_filepath)):
            tweet = status.item
            if not count % 100:
                log.debug("Processing %s tweets", count)
            if "text" in tweet or "full_text" in tweet:
                user_id = tweet["user"]["id_str"]
                if incremental:
                    # Update state
                    key = "timeline.{}.since_id".format(user_id)
                    self.state_store.set_state(__name__, key,
                                               max(self.state_store.get_state(__name__, key) or 0, tweet.get("id")))
                self._process_tweet(tweet)

    def _process_tweets(self, warc_iter):
        max_tweet_id = None

        for count, status in enumerate(warc_iter):
            tweet = status.item
            if not count % 100:
                log.debug("Processing %s tweets", count)
            if "text" in tweet or "full_text" in tweet:
                max_tweet_id = max(max_tweet_id or 0, tweet.get("id"))
                self._process_tweet(tweet)
        return max_tweet_id

    def _process_tweet(self, _):
        self.result.increment_stats("tweets")


if __name__ == "__main__":
    TwitterHarvester.main(TwitterHarvester, QUEUE, [SEARCH_ROUTING_KEY, TIMELINE_ROUTING_KEY])
