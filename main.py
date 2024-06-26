import os
import threading
from logger import log
from celery import Celery
from utils import LimitedSizeDict, strip_prefix_from_event
from prometheus_client import start_http_server, Summary, Gauge

# get settings from environment variables
PORT = int(os.environ.get("PORT", 8000))
BROKER_URL = os.environ.get("BROKER_URL", False)
BACKEND_URL = os.environ.get("BACKEND_URL", None)
if not BROKER_URL:
    log.error(f"BROKER_URL not set!")
    exit(-1)
# max_tasks_captured helps to limit memory footprint
MAX_TASKS_CAPTURED = int(os.environ.get("MAX_TASKS_CAPTURED", 100000))

# define size-limited dictionary with task information
queued_tasks = LimitedSizeDict(size_limit=MAX_TASKS_CAPTURED)

# define prometheus metrics
waiting_time = Summary('celery_queue_waiting_seconds', 'Waiting time of tasks in Celery queues', ['queue', 'task_name'])
queue_length = Gauge('celery_tasks_queue_length', 'Length of Celery queues with tasks', ['queue', 'task_name'])
tasks_running = Gauge('celery_tasks_running',
                      'Number of Celery Tasks currently running', ['queue', 'task_name', 'worker'])
tasks_captured = Gauge('celery_exporter_tasks_tracked',
                       'Number of tasks tracked by Celery exporter, limit is MAX_TASKS_CAPTURED')
tasks_captured.set_function(lambda: len(queued_tasks))
execution_stats = Summary('celery_tasks_duration_seconds',
                          'Duration of tasks when finished in given state', ['queue', 'task_name', 'worker', 'state'])

mutex = threading.Lock()

# this monitor captures celery events
def my_monitor(app):

    # event: task entered the queue
    def get_sent_time(event):
        with mutex:
            task_id = event['uuid']
            # timestamp, queue and taskname are only send at the sent-event
            queued_tasks[task_id] = {
                "ts": event['timestamp'],
                "queue": event['routing_key'],
                "taskname": event['name']
            }

            # This works because queued_tasks is a LimitedSizeDict which could possibly "reject" new keys being added.
            # Therefore we check if the key is stored so the information about this task is available in later steps.
            if task_id in queued_tasks:
                # only consider task in metrics if it has been stored in the dict
                queue_length.labels(queue=event['routing_key'], task_name=event['name']).inc()

    def get_started_time(event):
        with mutex:
            task_id = event['uuid']
            # check if tasks sent-event was captured previously
            if task_id in queued_tasks:
                # calculate waiting time in queue
                lag = event['timestamp'] - queued_tasks[task_id]['ts']
                # we have to use the information out of the dictionary,
                # because only the task-sent event stores queue information
                waiting_time.labels(queue=queued_tasks[task_id]['queue'],
                                    task_name=queued_tasks[task_id]['taskname']).observe(lag)
                queue_length.labels(queue=queued_tasks[task_id]['queue'],
                                    task_name=queued_tasks[task_id]['taskname']).dec()
                tasks_running.labels(queue=queued_tasks[task_id]['queue'],
                                     task_name=queued_tasks[task_id]['taskname'],
                                     worker=event['hostname']).inc()
                # set ts to timestamp of event for calculation of duration in last step
                queued_tasks[task_id]['ts'] = event['timestamp']

    def get_task_done(event):
        with mutex:
            task_id = event['uuid']
            # check if tasks sent-event was captured previously
            if task_id in queued_tasks:
                # we have to use the information out of the dictionary,
                # because only the task-sent event stores queue information
                tasks_running.labels(queue=queued_tasks[task_id]['queue'],
                                     task_name=queued_tasks[task_id]['taskname'],
                                     worker=event['hostname']).dec()
                # now add statistics of task execution to metrics
                runtime = event['timestamp'] - queued_tasks[task_id]['ts']
                execution_stats.labels(queue=queued_tasks[task_id]['queue'],
                                       task_name=queued_tasks[task_id]['taskname'],
                                       worker=event['hostname'],
                                       state=strip_prefix_from_event(event['type'])).observe(runtime)
                queued_tasks.pop(task_id, None)

    # register handler functions for task events in the receiver configuration
    with app.connection() as connection:
        recv = app.events.Receiver(connection, handlers={
                'task-sent': get_sent_time,
                'task-started': get_started_time,
                'task-succeeded': get_task_done,
                'task-failed': get_task_done,
                'task-revoked': get_task_done
        })
        # start receiver
        recv.capture(limit=MAX_TASKS_CAPTURED, timeout=None, wakeup=True)


if __name__ == '__main__':
    # http server will run in the background
    start_http_server(PORT)
    log.info(f"listening on port {PORT}")

    # initialize celery app for monitor
    app = Celery(broker=BROKER_URL, backend=BACKEND_URL)
    log.info(f"connected to Celery broker, capturing events...")
    my_monitor(app)
