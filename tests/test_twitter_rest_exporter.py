#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import absolute_import
import tests
from tests.tweets import tweet2
from twitter_rest_exporter import TwitterRestStatusTable
from datetime import datetime


class TestTwitterStatusTable(tests.TestCase):
    def test_row(self):
        table = TwitterRestStatusTable(None, None, None, None, None, None)
        row = table._row(tweet2)
        self.assertEqual("660065173563158529", row[0])
        self.assertEqual("https://twitter.com/justin_littman/status/660065173563158529", row[1])
        self.assertIsInstance(row[3], datetime)
        self.assertEqual("justin_littman", row[4])
        self.assertEqual("My new blog post on techniques for harvesting social media to WARCs: https://t.co/OHZki6pXEe",
                         row[5])
        self.assertEqual("original", row[6])
