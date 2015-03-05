# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import hashlib
import json
import os
import traceback
from abc import ABCMeta, abstractmethod
from multiprocessing import Manager

from ..testrunner import Stop

here = os.path.split(__file__)[0]

cache_manager = Manager()

def executor_kwargs(test_type, http_server_url, **kwargs):
    timeout_multiplier = kwargs["timeout_multiplier"]
    if timeout_multiplier is None:
        timeout_multiplier = 1

    executor_kwargs = {"http_server_url": http_server_url,
                       "timeout_multiplier": timeout_multiplier,
                       "debug_args": kwargs["debug_args"]}

    if test_type == "reftest":
        executor_kwargs["screenshot_cache"] = cache_manager.dict()

    return executor_kwargs


class TestharnessResultConverter(object):
    harness_codes = {0: "OK",
                     1: "ERROR",
                     2: "TIMEOUT"}

    test_codes = {0: "PASS",
                  1: "FAIL",
                  2: "TIMEOUT",
                  3: "NOTRUN"}

    def __call__(self, test, result):
        """Convert a JSON result into a (TestResult, [SubtestResult]) tuple"""
        assert result["test"] == test.url, ("Got results from %s, expected %s" %
                                            (result["test"], test.url))
        harness_result = test.result_cls(self.harness_codes[result["status"]], result["message"])
        return (harness_result,
                [test.subtest_result_cls(subtest["name"], self.test_codes[subtest["status"]],
                                         subtest["message"]) for subtest in result["tests"]])
testharness_result_converter = TestharnessResultConverter()


def reftest_result_converter(self, test, result):
    return (test.result_cls(result["status"], result["message"],
                            extra=result.get("extra")), [])


class ExecutorException(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message

class TestExecutor(object):
    __metaclass__ = ABCMeta

    test_type = None
    convert_result = None

    def __init__(self, browser, http_server_url, timeout_multiplier=1,
                 debug_args=None):
        """Abstract Base class for object that actually executes the tests in a
        specific browser. Typically there will be a different TestExecutor
        subclass for each test type and method of executing tests.

        :param browser: ExecutorBrowser instance providing properties of the
                        browser that will be tested.
        :param http_server_url: Base url of the http server on which the tests
                                are running.
        :param timeout_multiplier: Multiplier relative to base timeout to use
                                   when setting test timeout.
        """
        self.runner = None
        self.browser = browser
        self.http_server_url = http_server_url
        self.timeout_multiplier = timeout_multiplier
        self.debug_args = debug_args
        self.protocol = None # This must be set in subclasses

    @property
    def logger(self):
        """StructuredLogger for this executor"""
        if self.runner is not None:
            return self.runner.logger

    def setup(self, runner):
        """Run steps needed before tests can be started e.g. connecting to
        browser instance

        :param runner: TestRunner instance that is going to run the tests"""
        self.runner = runner
        self.protocol.setup(runner)

    def teardown(self):
        """Run cleanup steps after tests have finished"""
        self.protocol.teardown()

    def run_test(self, test):
        """Run a particular test.

        :param test: The test to run"""
        try:
            result = self.do_test(test)
        except Exception as e:
            result = self.result_from_exception(test, e)

        if result is Stop:
            return result

        if result[0].status == "ERROR":
            self.logger.debug(result[0].message)
        self.runner.send_message("test_ended", test, result)

    @abstractmethod
    def do_test(self, test):
        """Test-type and protocol specific implmentation of running a
        specific test.

        :param test: The test to run."""
        pass

    def result_from_exception(self, test, e):
        if hasattr(e, "status") and e.status in test.result_cls.statuses:
            status = e.status
        else:
            status = "ERROR"
        message = unicode(getattr(e, "message", ""))
        if message:
            message += "\n"
        message += traceback.format_exc(e)
        return test.result_cls(status, message), []


class TestharnessExecutor(TestExecutor):
    convert_result = testharness_result_converter


class RefTestExecutor(TestExecutor):
    convert_result = reftest_result_converter

    def __init__(self, browser, http_server_url, timeout_multiplier=1, screenshot_cache=None,
                 debug_args=None):
        TestExecutor.__init__(self, browser, http_server_url,
                              timeout_multiplier=timeout_multiplier,
                              debug_args=debug_args)

        self.screenshot_cache = screenshot_cache

class RefTestImplementation(object):
    def __init__(self, executor):
        self.timeout_multiplier = executor.timeout_multiplier
        self.executor = executor
        # Cache of url:(screenshot hash, screenshot). Typically the
        # screenshot is None, but we set this value if a test fails
        # and the screenshot was taken from the cache so that we may
        # retrieve the screenshot from the cache directly in the future
        self.screenshot_cache = self.executor.screenshot_cache
        self.message = None

    @property
    def logger(self):
        return self.executor.logger

    def get_hash(self, url, timeout):
        timeout = timeout * self.timeout_multiplier

        if url not in self.screenshot_cache:
            success, data = self.executor.screenshot(url, timeout)

            if not success:
                return False, data

            screenshot = data
            hash_value = hashlib.sha1(screenshot).hexdigest()

            self.screenshot_cache[url] = (hash_value, None)

            rv = True, (hash_value, screenshot)
        else:
            rv = True, self.screenshot_cache[url]

        self.message.append("%s %s" % (url, rv[1][0]))
        return rv

    def is_pass(self, lhs_hash, rhs_hash, relation):
        assert relation in ("==", "!=")
        self.message.append("Testing %s %s %s" % (lhs_hash, relation, rhs_hash))
        return ((relation == "==" and lhs_hash == rhs_hash) or
                (relation == "!=" and lhs_hash != rhs_hash))

    def run_test(self, test):
        self.message = []

        # Depth-first search of reference tree, with the goal
        # of reachings a leaf node with only pass results

        stack = list(((test, item[0]), item[1]) for item in reversed(test.references))
        while stack:
            hashes = [None, None]
            screenshots = [None, None]

            nodes, relation = stack.pop()

            for i, node in enumerate(nodes):
                success, data = self.get_hash(node.url, node.timeout)
                if success is False:
                    return {"status": data[0], "message": data[1]}

                hashes[i], screenshots[i] = data

            if self.is_pass(hashes[0], hashes[1], relation):
                if nodes[1].references:
                    stack.extend(list(((nodes[1], item[0]), item[1]) for item in reversed(nodes[1].references)))
                else:
                    # We passed
                    return {"status":"PASS", "message": None}

        for i, (node, screenshot) in enumerate(zip(nodes, screenshots)):
            if screenshot is None:
                success, screenshot = self.retake_screenshot(node)
                if success:
                    screenshots[i] = screenshot

        log_data = [{"url": nodes[0].url, "screenshot": screenshots[0]}, relation,
                    {"url": nodes[1].url, "screenshot": screenshots[1]}]

        return {"status": "FAIL",
                "message": "\n".join(self.message),
                "extra": {"reftest_screenshots": log_data}}

    def retake_screenshot(self, node):
        success, data = self.executor.screenshot(node.url,
                                                 node.timeout *
                                                 self.timeout_multiplier)
        if not success:
            return False, data

        hash_val, _ = self.screenshot_cache[node.url]
        self.screenshot_cache[node.url] = hash_val, data
        return True, data

class Protocol(object):
    def __init__(self, executor, browser, http_server_url):
        self.executor = executor
        self.browser = browser
        self.http_server_url = http_server_url

    @property
    def logger(self):
        return self.executor.logger

    def setup(self, runner):
        pass

    def teardown(self):
        pass

    def wait(self):
        pass
