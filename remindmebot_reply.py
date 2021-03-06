#!/usr/bin/env python3.6

# =============================================================================
# IMPORTS
# =============================================================================

import praw
import MySQLdb
import traceback
from threading import Thread, Lock
import os
import sys
import configparser
import time
import requests
import logging
from datetime import datetime
from requests.exceptions import HTTPError, ConnectionError, Timeout
from praw.exceptions import APIException, ClientException, PRAWException
from socket import timeout

# =============================================================================
# GLOBALS
# =============================================================================

# Reads the config file
config = configparser.ConfigParser()
config.read("remindmebot.cfg")

bot_username = config.get("Reddit", "username")
bot_password = config.get("Reddit", "password")
client_id = config.get("Reddit", "client_id")
client_secret = config.get("Reddit", "client_secret")

#Reddit info
reddit = praw.Reddit(client_id=client_id,
                     client_secret=client_secret,
                     password=bot_password,
                     user_agent='cryptoRemindMe by /u/BoyAndHisBlob',
                     username=bot_username)
# DB Info
DB_USER = config.get("SQL", "user")
DB_PASS = config.get("SQL", "passwd")

ENVIRONMENT = config.get("REMINDME", "environment")
DEV_USER_NAME = "BoyAndHisBlob"

FORMAT = '%(asctime)-15s %(message)s'
logging.basicConfig(format=FORMAT)
logger = logging.getLogger('cryptoRemindMeBot')
logger.setLevel(logging.INFO)

supported_tickers = ["ADA","BCH","BCN","BTC","BTG","DASH","DOGE","ETC","ETH","LSK","LTC","NEO","QASH","QTUM","REQ",
                     "STEEM","XEM","XLM","XMR","XRB","XRP","ZEC"]

MAX_API_TIME_LIMIT = 2000
cc_max_api_per_sec = 15
cc_total_api_calls = 0
cc_api_lock = Lock()

# =============================================================================
# CLASSES
# =============================================================================

class DbConnectiton(object):
    """
    DB connection class
    """
    connection = None
    cursor = None

    def __init__(self):
        self.connection = MySQLdb.connect(
            host="localhost", user=DB_USER, passwd=DB_PASS, db="crypto_remind_me"
        )
        self.cursor = self.connection.cursor()

class Reply(object):

    def __init__(self):
        self._db_connection = DbConnectiton()
        self._replyMessage =(
            "cryptoRemindMeBot private message here!" 
            "\n\n**The message:** \n\n>{message}"
            "\n\n**The original comment:** \n\n>{original}"
            "\n\n**The parent comment from the original comment or its submission:** \n\n>{parent}"
            "{origin_date_text}"
            "\n\nYou requested a reminder when the price of {ticker} reached {new_price} from {origin_price}."
            "\n\nThe price hit {price} at {price_time} using CryptoCompare's Current Aggregate."
            "\n\n_____\n\n"
            "^| [^(README)](https://github.com/jjmerri/cryptoRemindMe-Reddit/blob/master/README.md)"
            " ^| [^(Your Reminders)](http://np.reddit.com/message/compose/?to=cryptoRemindMeBot&subject=List Of Reminders&message=MyReminders!)"
            " ^| [^(Feedback)](http://np.reddit.com/message/compose/?to=BoyAndHisBlob&subject=cryptoRemindMe Feedback)"
            " ^| [^(Code)](https://github.com/jjmerri/cryptoRemindMe-Reddit)"
            " ^| [^(Tip BoyAndHisBlob)](https://blobware-tips.firebaseapp.com)"
            )
        self.last_price_time = {}
        self._price_history = {}
        self.lock = Lock()


    def set_price_extremes(self):
        """
        Sets the high and low since the last run
        """
        global cc_total_api_calls

        #reset number of calls for new iteration
        cc_total_api_calls = 0

        lastrun_file = open("lastrun.txt", "r")
        lastrun_sec = {}
        for lastrun in lastrun_file.read().splitlines():
            values = lastrun.split(" ")
            if len(values) == 2:
                lastrun_sec[values[0]] = int(values[1])

        lastrun_file.close()

        price_threads = [];

        for supported_ticker in supported_tickers:
            current_time_sec = int(time.time())
            if supported_ticker in lastrun_sec:
                mins_since_lastrun = (current_time_sec - lastrun_sec[supported_ticker]) // 60
            else:
                #max allowed by API
                mins_since_lastrun = MAX_API_TIME_LIMIT

            if mins_since_lastrun > MAX_API_TIME_LIMIT:
                mins_since_lastrun = MAX_API_TIME_LIMIT

            #Get data from at least 10 min back
            mins_since_lastrun = mins_since_lastrun if mins_since_lastrun >= 10 else 10

            #Thread api calls because they take a while in succession
            t = Thread(target=self._update_price_data, args=[supported_ticker, mins_since_lastrun])
            price_threads.append(t)
            t.start()

        #Wait for all price data to be set
        for price_thread in price_threads:
            price_thread.join()

        for supported_ticker in supported_tickers:
            if supported_ticker in self._price_history: #false if we couldnt retriev price data from the API
                for minute_data in self._price_history[supported_ticker]:

                    high = minute_data['high']
                    low = minute_data['low']

                    #sometimes the API incorrectly returns 0 so this is an attempt to avoid incorrectly notifying on 0 price
                    if high > 0 and low > 0:
                        if (supported_ticker + "_high") not in self._price_history or high > self._price_history[supported_ticker + "_high"]:
                            self._price_history[supported_ticker + "_high"] = high
                            self._price_history[supported_ticker + "_high_time"] = minute_data['time']

                        if (supported_ticker + "_low") not in self._price_history or low < self._price_history[supported_ticker + "_low"]:
                            self._price_history[supported_ticker + "_low"] = low
                            self._price_history[supported_ticker + "_low_time"] = minute_data['time']

                        if supported_ticker not in self.last_price_time or minute_data['time'] > self.last_price_time[supported_ticker]:
                            self.last_price_time[supported_ticker] = minute_data['time']

    def _update_price_data(self, ticker, limit):
        """
        :param ticker: the ticker for the crypto that the price info is being updated for
        :param limit: number of minutes back to get the price data
        """
        global cc_total_api_calls

        api_url = 'https://min-api.cryptocompare.com/data/histominute?fsym={ticker}&tsym=USD&e=CCCAGG&limit={limit}'.format(
            ticker = ticker, limit = str(limit))

        api_error_count = 0

        #Loop to retry getting API data. Will break on success or 10 consecutive errors
        while True:
            #if we exceed the allowed number of api calls wait and reset counters
            #wait is set to 2 seconds because 1 isnt enough for some reason
            #even though the API says 15 calls per second
            with cc_api_lock:
                if cc_total_api_calls >= cc_max_api_per_sec:
                    time.sleep(2)
                    cc_total_api_calls = 0

                cc_total_api_calls += 1

            r = requests.get(api_url)
            response = r.json()

            #If not success then retry up to 10 times after 1 sec wait
            if response.get("Response", "Error") != "Success":
                api_error_count += 1
                print("Retry number {error_count} Retrieving {ticker} Info".format(ticker = ticker, error_count = api_error_count))
                time.sleep(1)
                if api_error_count >= 10:
                    send_dev_pm("Error Retrieving {ticker} Info".format(ticker = ticker),
                                "Max error count hit. Could not retrieve histo info from {url}".format(url = api_url))
                    break #dont infinite loop. Maybe the API is down.
            else:
                break

        #Not sure if this lock is necessary but it makes me feel better and adds little overhead
        with self.lock:
            self._price_history[ticker] = response['Data']

    def _parent_comment(self, commentId):
        """
        Returns the parent comment or if it's a top comment
        return the original submission
        """
        try:
            comment = reddit.comment(id=commentId)
            if comment.is_root:
                return comment.submission.permalink
            else:
                return comment.parent().permalink
        except IndexError as err:
            logger.exception("parrent_comment error")
            return "It seems your original comment was deleted, unable to return parent comment."
        # Catch any URLs that are not reddit comments
        except Exception  as err:
            logger.exception("HTTPError/PRAW parent comment")
            return "Parent comment not required for this URL."

    def populate_reply_list(self):
        """
        Checks to see through SQL if net_date is < current time
        """
        select_statement = "SELECT * FROM reminder WHERE "
        single_where_clause = "(new_price <= %s AND new_price >= origin_price AND ticker = %s) OR (new_price >= %s AND new_price <= origin_price AND ticker = %s)"
        where_clause = ((single_where_clause + " OR ") * len(supported_tickers))[0:-4]
        cmd = select_statement + where_clause

        cmd_args = []

        for supported_ticker in supported_tickers:
            if (supported_ticker + "_high") in self._price_history and (supported_ticker + "_low") in self._price_history:
                cmd_args.append(self._price_history[supported_ticker + "_high"])
                cmd_args.append(supported_ticker)
                cmd_args.append(self._price_history[supported_ticker + "_low"])
                cmd_args.append(supported_ticker)
            else:
                #remove a where clause + " AND "
                cmd_minus_where_length = len(single_where_clause) + 4
                cmd = cmd[:(cmd_minus_where_length * -1)]

        self._db_connection.cursor.execute(cmd, cmd_args)

    def send_replies(self):
        """
        Loop through data looking for which comments are old
        """

        data = self._db_connection.cursor.fetchall()
        already_commented = []
        for row in data:
            # checks to make sure ID hasn't been commented already
            # For situtations where errors happened
            if row[0] not in already_commented:
                ticker = row[9]
                object_name = row[1]
                new_price = row[3]
                origin_price = row[4]
                comment_create_datetime = row[10]

                send_reply = False
                message_price = 0.0
                message_price_time = 0

                try:
                    for minute_data in self._price_history[ticker]:
                        price_high = minute_data['high']
                        price_low = minute_data['low']
                        price_time = minute_data['time']

                        if price_time >= comment_create_datetime.timestamp():
                            if new_price <= price_high and new_price >= origin_price:
                                message_price = price_high
                            elif new_price >= price_low and new_price <= origin_price:
                                message_price = price_low
                            else:
                                # This is not the minute_data that triggered the reply
                                continue

                            message_price_time = price_time
                            send_reply = True
                            break

                except IndexError as err:
                    logger.exception("IndexError in send_replies")
                    send_reply = False
                # Catch any URLs that are not reddit comments
                except Exception  as err:
                    logger.exception("Unknown Exception send_replies")
                    send_reply = False

                if send_reply:
                    # MySQl- object_name, message, comment_create_datetime, reddit user, new_price, origin_price, permalink, ticker, message_price_time, message_price
                    delete_message = self._send_reply(object_name, row[2], comment_create_datetime, row[5], new_price, origin_price, row[8], ticker, message_price_time, message_price)
                    if delete_message:
                        cmd = "DELETE FROM reminder WHERE id = %s"
                        self._db_connection.cursor.execute(cmd, [row[0]])
                        self._db_connection.connection.commit()
                        already_commented.append(row[0])

        self._db_connection.connection.commit()
        self._db_connection.connection.close()

    def _send_reply(self, object_name, message, comment_create_datetime, author, new_price, origin_price, permalink, ticker, message_price_time, message_price):
        """
        Replies a second time to the user after a set amount of time
        """
        logger.info("---------------")
        logger.info(author)
        logger.info(object_name)

        utc_create_date_str = str(datetime.utcfromtimestamp(comment_create_datetime.timestamp()))
        origin_date_text =  ("\n\nYou requested this reminder on: " 
                            "[" + utc_create_date_str + " UTC](http://www.wolframalpha.com/input/?i="
                             + utc_create_date_str + " UTC To Local Time)")

        message_price_datetime = datetime.utcfromtimestamp(message_price_time)
        message_price_datetime_formatted = ("[" + format(message_price_datetime, '%Y-%m-%d %H:%M:%S') + " UTC](http://www.wolframalpha.com/input/?i="
                             + format(message_price_datetime, '%Y-%m-%d %H:%M:%S') + " UTC To Local Time)")

        try:
            reddit.redditor(str(author)).message('cryptoRemindMeBot Reminder!', self._replyMessage.format(
                    message=message,
                    original=permalink,
                    parent= self._parent_comment(object_name),
                    origin_date_text = origin_date_text,
                    new_price = '${:,.4f}'.format(new_price),
                    origin_price = '${:,.4f}'.format(origin_price),
                    price = '${:,.4f}'.format(message_price),
                    price_time = message_price_datetime_formatted,
                    ticker = ticker
                ))
            logger.info("Sent Reply")
            return True
        except APIException as err:
            logger.exception("APIException in _send_reply")
            return False
        except IndexError as err:
            logger.exception("IndexError in _send_reply")
            return False
        except (HTTPError, ConnectionError, Timeout, timeout) as err:
            logger.exception("HTTPError in _send_reply")
            time.sleep(10)
            return False
        except ClientException as err:
            logger.exception("ClientException in _send_reply")
            time.sleep(10)
            return False
        except PRAWException as err:
            logger.exception("PRAWException in _send_reply")
            time.sleep(10)
            return False
        except Exception as err:
            logger.exception("Unknown Exception in _send_reply")
            return False

def update_last_run(checkReply):
    lastrun_tickers = ""
    for supported_ticker in supported_tickers:
        # dont let 0 get into the lastrun.txt. It breaks the api call to get the prices
        if supported_ticker not in checkReply.last_price_time:
            checkReply.last_price_time[supported_ticker] = 10000

        lastrun_tickers += supported_ticker + " " + str(
            checkReply.last_price_time.get(supported_ticker, "10000")) + "\n"

    lastrun_file = open("lastrun.txt", "w")
    lastrun_file.write(lastrun_tickers)
    lastrun_file.close()

def create_running():
    running_file = open("reply_bot.running", "w")
    running_file.write(str(os.getpid()))
    running_file.close()

def send_dev_pm(subject, body):
    reddit.redditor(DEV_USER_NAME).message(subject, body)


# =============================================================================
# MAIN
# =============================================================================

def main():
    logger.info("start")
    start_process = False

    if ENVIRONMENT == "DEV":
        os.remove("reply_bot.running")
        logger.info("running file removed")

    if not os.path.isfile("reply_bot.running"):
        create_running()
        start_process = True
    else:
        start_process = False
        logger.error("Reply already running! Will not start.")

    while start_process and os.path.isfile("reply_bot.running"):
        try:
            logger.info("Start Main Loop")
            checkReply = Reply()
            checkReply.set_price_extremes()
            checkReply.populate_reply_list()
            checkReply.send_replies()

            update_last_run(checkReply)
            logger.info("End Main Loop")
            time.sleep(600)
        except Exception as err:
            logger.exception("Unknown Exception in main loop")
            try:
                send_dev_pm("Unknown Exception in main loop", "Error: {exception}\n\n{trace}".format(exception = str(err), trace = traceback.format_exc()))
            except Exception as err:
                logger.exception("Unknown senidng dev pm")

            time.sleep(600)

    sys.exit()

# =============================================================================
# RUNNER
# =============================================================================
if __name__ == '__main__':
    main()
