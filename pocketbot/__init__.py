"""pocketbot — a fixed-time (binary options) bot for Pocket Option / Pocket Broker.

Paper by default. It talks to the broker through the unofficial
BinaryOptionsToolsV2 websocket client, because Pocket Option publishes no
trading API. See docs/POCKETBOT.md for the research behind the design and the
maths that decides whether any of this can make money.
"""

__version__ = "0.1.0"
