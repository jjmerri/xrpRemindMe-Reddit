#!/bin/bash
script_dir="/root/apps/cryptoRemindMe-Reddit/"
search_running_file=$script_dir"search_bot.running"
reply_running_file=$script_dir"reply_bot.running"

mail_sent_file=$script_dir"monitor_mail_sent.txt"

search_log_file=$script_dir"search.log"
reply_log_file=$script_dir"reply.log"

search_pid=`cat $search_running_file`
reply_pid=`cat $reply_running_file`

kill -0 $search_pid
kill_search_ret=$?

kill -0 $reply_pid
kill_reply_ret=$?

if [ "$kill_reply_ret" -ne "0" -o "$kill_search_ret" -ne "0" ] && [ ! -f $mail_sent_file ]
then
    echo "mail sent" > $mail_sent_file
    (echo "SEARCH LOG"; tail -40 $search_log_file; echo "REPLY LOG"; tail -40 $reply_log_file) | mail -t jjmerri88@gmail.com -s "Crypto Remind Me Bots Not Running!"
fi
