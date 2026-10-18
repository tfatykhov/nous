"""F100: browser_task — an isolated browser-use agent loop.

The main agent calls the `browser_task` tool; a separate inner loop drives a
headless Chromium through Anthropic's browser toolset and hands back only a
bounded text summary. Raw page content never reaches the main reasoning loop.
"""
