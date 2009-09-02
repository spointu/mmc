# -*- coding: utf-8; -*-
#
# (c) 2008 Mandriva, http://www.mandriva.com/
#
# $Id$
#
# This file is part of Pulse 2, http://pulse2.mandriva.org
#
# Pulse 2 is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Pulse 2 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Pulse 2; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301, USA.

# Big modules
import logging
import time
import re
import os
import random
import datetime

import sqlalchemy
import sqlalchemy.orm

# Twisted modules
import twisted.internet

# MMC plugins
from pulse2.database.msc import MscDatabase
import pulse2.apis.clients.mirror

# ORM mappings
from pulse2.database.msc.orm.commands import Commands
from pulse2.database.msc.orm.commands_on_host import CommandsOnHost
from pulse2.database.msc.orm.commands_history import CommandsHistory
from pulse2.database.msc.orm.target import Target
from pulse2.database.utilities import handle_deconnect

# our modules
from pulse2.consts import *
from pulse2.scheduler.config import SchedulerConfig
from pulse2.scheduler.launchers_driving import callOnBestLauncher, callOnLauncher, getLaunchersBalance, probeClient
import pulse2.scheduler.network
from pulse2.scheduler.assign_algo import MGAssignAlgoManager
from pulse2.scheduler.checks import getCheck, getAnnounceCheck
from pulse2.scheduler.launchers_driving import pingAndProbeClient
from pulse2.scheduler.tracking.proxy import LocalProxiesUsageTracking
from pulse2.scheduler.tracking.commands import CommandsOnHostTracking

handle_deconnect()

def gatherStuff():
    """ handy function to gather widely used objects """
    session = sqlalchemy.orm.create_session()
    database = MscDatabase()
    logger = logging.getLogger()
    return (session, database, logger)

def gatherCoHStuff(idCommandOnHost):
    """ same as gatherStuff(), this time for a particular CommandOnHost """
    session = sqlalchemy.orm.create_session()
    database = MscDatabase()
    myCommandOnHost = session.query(CommandsOnHost).get(idCommandOnHost)
    if type(myCommandOnHost) != CommandsOnHost:
        session.close()
        logging.getLogger().error("trying to gather CoH on an inexisting CoH '%s' !! (Maybe you are currently cleaning the database?)"%(str(idCommandOnHost)))
        return (None, None, None)
    myCommand = session.query(Commands).get(myCommandOnHost.getIdCommand())
    myTarget = session.query(Target).get(myCommandOnHost.getIdTarget())
    session.close()
    if type(myCommand) != Commands or type(myTarget) != Target:
        logging.getLogger().error("trying to gather CoH on an inexisting CoH '%s' !! (Maybe you are currently cleaning the database?)"%(str(idCommandOnHost)))
        return (None, None, None)
    return (myCommandOnHost, myCommand, myTarget)

def isLastToInventoryInBundle(myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return []

    session = sqlalchemy.orm.create_session()
    database = MscDatabase()

    nb = session.query(CommandsOnHost
        ).select_from(database.commands_on_host.join(database.commands).join(database.target)
        ).filter(database.commands.c.fk_bundle == myC.fk_bundle
        ).filter(database.commands.c.order_in_bundle == myC.order_in_bundle
        ).filter(database.target.c.target_uuid ==  myT.target_uuid
        ).filter(sqlalchemy.not_(
            database.commands_on_host.c.current_state.in_(PULSE2_POST_INVENTORY_STATES))
        ).count()

    session.close()
    if nb != 1:
        logging.getLogger().debug("isLastToInventoryInBundle on #%s : still %s coh in the same bundle to do" % (str(myCommandOnHostID), str(nb-1)))
        return False
    return True

def isLastToHaltInBundle(myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return []

    session = sqlalchemy.orm.create_session()
    database = MscDatabase()

    nb = session.query(CommandsOnHost
        ).select_from(database.commands_on_host.join(database.commands).join(database.target)
        ).filter(database.commands.c.fk_bundle == myC.fk_bundle
        ).filter(database.commands.c.order_in_bundle == myC.order_in_bundle
        ).filter(database.target.c.target_uuid ==  myT.target_uuid
        ).filter(sqlalchemy.not_(
            database.commands_on_host.c.current_state.in_(PULSE2_POST_HALT_STATES))
        ).count()

    session.close()
    if nb > 1:
        logging.getLogger().debug("isLastToHaltInBundle on #%s : still %s coh in the same bundle to do" % (str(myCommandOnHostID), str(nb-1)))
        return False
    return True

def getDependancies(myCommandOnHostID):
    """
        check deps, returns;
            "dead": will never be able to complete
            "wait": may be able to complet, but not yet
            "run": can run now
    """

    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return []

    session = sqlalchemy.orm.create_session()
    database = MscDatabase()

    # look for CoH from same bundle
    # look for CoH from lower order
    # look for unfinished CoH
    # look for CoH on same host
    coh_finished_deps = [] # mostly deps with 'done' status
    coh_unfinishable_deps = [] # deps with 'failed' or 'over_timed' status
    coh_relevant_deps = [] # other deps

    for q in session.query(CommandsOnHost
        ).select_from(database.commands_on_host.join(database.commands).join(database.target)
        ).filter(database.commands.c.fk_bundle == myC.fk_bundle
        ).filter(database.commands.c.order_in_bundle < myC.order_in_bundle
        ).filter(database.target.c.target_uuid ==  myT.target_uuid
        ).all():
            if q.current_state in ['done']:
                coh_finished_deps.append(q.id)
            elif q.current_state  in ['failed', 'over_timed']:
                coh_unfinishable_deps.append(q.id)
            else:
                coh_relevant_deps.append(q.id)
    session.close()


    if len(coh_unfinishable_deps) > 0: # at least one dep will never be complete, give up
        logging.getLogger().debug("command_on_host #%s: was waiting on %s, all of them beeing unfinishable" % (myCoH.getId(), coh_unfinishable_deps))
        return 'dead'

    if len(coh_relevant_deps) > 0: # at least one dep should be completed, wait
        logging.getLogger().debug("command_on_host #%s: was waiting on %s, to be completed" % (myCoH.getId(), coh_relevant_deps))
        return 'wait'

    logging.getLogger().debug("command_on_host #%s: was waiting on %s, all of them beeing completed" % (myCoH.getId(), coh_finished_deps))
    return 'run'

def localProxyUploadStatus(myCommandOnHostID):
    """ attempt to analyse coh in the same command in order to now how we may advance.
    possible return values:
        - 'waiting': my time is not yet come
        - 'server': I'm an active proxy server
        - 'dead': I'm a client and all proxies seems dead
        - 'error': Something wrong was found in the command (usually mess in priorities)
        - an int: I'm a client and the returned value is the CoH I will use
    """

    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return 'error'

    # as for now, if we previously found a proxy, use it
    # commented out: may break the split proxy model
    #if myCoH.getUsedProxy() != None:
    #    logging.getLogger().debug("scheduler %s: keeping coh #%s as local proxy for #%s" % (SchedulerConfig().name, myCoH.getUsedProxy(), myCommandOnHostID))
    #    return 'keeping'

    # see what to do next
    proxy_mode = getProxyModeForCommand(myCommandOnHostID)
    if proxy_mode == 'queue':
        return localProxyAttemptQueueMode(myCommandOnHostID)
    elif proxy_mode == 'split':
        return localProxyAttemptSplitMode(myCommandOnHostID)
    else:
        logging.getLogger().debug("scheduler %s: command #%s seems to be wrong (bad priorities ?)" % (SchedulerConfig().name, myC.id))
        return 'dead'

def localProxyAttemptQueueMode(myCommandOnHostID):
    # queue mode (serial) implementation of proxy mode
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return 'error'

    smallest_done_upload_order_in_proxy = None
    best_ready_proxy_server_coh = None
    potential_proxy_server_coh = None

    # iterate over CoH which
    # are linked to the same command
    # are not our CoH
    session = sqlalchemy.orm.create_session()
    database = MscDatabase()
    for q in session.query(CommandsOnHost).\
        select_from(database.commands_on_host.join(database.commands).join(database.target)).\
        filter(database.commands.c.id == myC.id).\
        filter(database.commands_on_host.c.id != myCoH.getId()).\
        all():
            if q.uploaded == PULSE2_STAGE_DONE:                                 # got a pal which succeeded in doing its upload
                if q.order_in_proxy != None:                                    # got a potent proxy server
                    if smallest_done_upload_order_in_proxy < q.order_in_proxy:  # keep its id as it seems to be the best server ever
                        smallest_done_upload_order_in_proxy = q.order_in_proxy
                        best_ready_proxy_server_coh = q.id
            elif q.current_state != 'failed':                                   # got a pal which may still do something
                if q.order_in_proxy != None:                                    # got a potential proxy server
                    if myCoH.order_in_proxy == None:                            # i may use this server, as I'm not server myself
                        potential_proxy_server_coh = q.id
                    elif myCoH.order_in_proxy > q.order_in_proxy:               # i may use this server, as it has a lower priority than me
                        potential_proxy_server_coh = q.id
    session.close()

    # we now know:
    # a proxy that may be used
    # a proxy that might be used
    # let's take a decision about our future

    if myCoH.getOrderInProxy() == None:                                 # I'm a client: I MUST use a proxy server ...
        if best_ready_proxy_server_coh != None:                         # ... and a proxy seems ready => PROXY CLIENT MODE
            (current_client_number, max_client_number) = getClientUsageForProxy(best_ready_proxy_server_coh)
            if current_client_number < max_client_number:
                logging.getLogger().debug("scheduler %s: found coh #%s as local proxy for #%s" % (SchedulerConfig().name, best_ready_proxy_server_coh, myCommandOnHostID))
                return best_ready_proxy_server_coh
            else:
                logging.getLogger().debug("scheduler %s: found coh #%s as local proxy for #%s, but proxy is full (%d clients), so I'm waiting" % (SchedulerConfig().name, best_ready_proxy_server_coh, myCommandOnHostID, current_client_number))
                return 'waiting'
        elif potential_proxy_server_coh != None:                        # ... and one may become ready => WAITING
            logging.getLogger().debug("scheduler %s: coh #%s still waiting for a local proxy to use" % (SchedulerConfig().name, myCommandOnHostID))
            return 'waiting'
        else:                                                           # ... but all seems dead => ERROR
            logging.getLogger().debug("scheduler %s: coh #%s won't likely be able to use a local proxy" % (SchedulerConfig().name, myCommandOnHostID))
            return 'dead'
    else:                                                               # I'm a server: I MAY use a proxy ...
        if best_ready_proxy_server_coh != None:                         # ... and a proxy seems ready => PROXY CLIENT MODE
            (current_client_number, max_client_number) = getClientUsageForProxy(best_ready_proxy_server_coh)
            if current_client_number < max_client_number:
                logging.getLogger().debug("scheduler %s: found coh #%s as local proxy for #%s" % (SchedulerConfig().name, best_ready_proxy_server_coh, myCommandOnHostID))
                return best_ready_proxy_server_coh
            else:
                logging.getLogger().debug("scheduler %s: found coh #%s as local proxy for #%s, but proxy is full (%d clients), so I'm waiting" % (SchedulerConfig().name, best_ready_proxy_server_coh, myCommandOnHostID, current_client_number))
                return 'waiting'
        elif potential_proxy_server_coh:                                # ... but a better candidate may become ready => WAITING
            logging.getLogger().debug("scheduler %s: coh #%s still waiting to know if is is local proxy client or server" % (SchedulerConfig().name, myCommandOnHostID))
            return 'waiting'
        else:                                                           # ... and other best candidates seems dead => PROXY SERVER MODE
            logging.getLogger().debug("scheduler %s: coh #%s become local proxy server" % (SchedulerConfig().name, myCommandOnHostID))
            return 'server'

def localProxyAttemptSplitMode(myCommandOnHostID):
    # split mode (parallel) implementation of proxy mode

    def __processProbes(result):
        # remove bad proxy (result => alive_proxy):
        alive_proxies = dict()
        for (success, (probe, uuid, coh_id)) in result:
            if success: # XMLRPC call do succeedeed
                if probe:
                    if probe != "Not available":
                        alive_proxies[uuid] = coh_id

        # map if to go from {uuid1: (coh1, max1), uuid2: (coh2, max2)} to ((uuid1, max1), (uuid2, max2))
        # ret val is an uuid
        final_uuid = LocalProxiesUsageTracking().take_one(alive_proxies.keys(), myC.getId())
        if not final_uuid: # not free proxy, wait
            logging.getLogger().debug("scheduler %s: coh #%s wait for a local proxy for to be usable" % (SchedulerConfig().name, myCommandOnHostID))
            return 'waiting'
        else: # take a proxy in alive proxies
            final_proxy = alive_proxies[final_uuid]
            logging.getLogger().debug("scheduler %s: coh #%s found coh #%s as local proxy, taking one slot (%d left)" % (SchedulerConfig().name, myCommandOnHostID, final_proxy, LocalProxiesUsageTracking().how_much_left_for(final_uuid, myC.getId())))
            return final_proxy

    def __processProbe(result, uuid, proxy):
        logging.getLogger().debug("scheduler %s: coh #%s probed on %s, got %s" % (SchedulerConfig().name, proxy, uuid, result))
        return (result, uuid, proxy)

    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return 'error'
    if myCoH.getOrderInProxy() == None:                                 # I'm a client: I MUST use a proxy server ...
        temp_dysfunc_proxy = list() # proxies with no data (UPLOADED != DONE)
        def_dysfunc_proxy = list()  # proxies with no data (UPLOADED != DONE) and which definitely wont't process further (current_state != scheduled)
        available_proxy = list()    # proxies with complete data (UPLOADED = DONE)

        # iterate over CoH which
        # are linked to the same command
        # are not our CoH
        # are proxy server
        session = sqlalchemy.orm.create_session()
        database = MscDatabase()
        for q in session.query(CommandsOnHost).\
            select_from(database.commands_on_host.join(database.commands).join(database.target)).\
            filter(database.commands.c.id == myC.id).\
            filter(database.commands_on_host.c.id != myCoH.getId()).\
            filter(database.commands_on_host.c.order_in_proxy != None).\
            all():
                # got 4 categories here:
                #  - DONE and not DONE
                #  - scheduled and not scheduled
                # => upload DONE, (scheduled or not): proxy free to use (depnding on nb of clients, see below)
                # => upload !DONE + (failed, over_timed) => will never be available => defin. failed
                # => upload !DONE + ! (failed or over_timed) => may be available in some time => temp. failed
                if q.current_state in ('failed', 'over_timed', 'stopped', 'stop'):
                    def_dysfunc_proxy.append(q.id)
                elif q.uploaded == PULSE2_STAGE_DONE:
                    available_proxy.append(q.id)
                else:
                    temp_dysfunc_proxy.append(q.id)
        session.close()

        if len(available_proxy) == 0: # not proxy seems ready ?
            if len(temp_dysfunc_proxy) == 0: # and others seems dead
                logging.getLogger().debug("scheduler %s: coh #%s won't likely be able to use a local proxy" % (SchedulerConfig().name, myCommandOnHostID))
                return 'dead'
            else:
                logging.getLogger().debug("scheduler %s: coh #%s wait for a local proxy to be ready" % (SchedulerConfig().name, myCommandOnHostID))
                return 'waiting'

        deffered_list = list()

        for proxy in available_proxy: # proxy is the proxy coh id
            (proxyCoH, proxyC, proxyT) = gatherCoHStuff(proxy)
            d = probeClient(
                proxyT.getUUID(),
                proxyT.getFQDN(),
                proxyT.getShortName(),
                proxyT.getIps(),
                proxyT.getMacs()
            )
            d.addCallback(__processProbe, proxyT.getUUID(), proxy)
            deffered_list.append(d)
        dl = twisted.internet.defer.DeferredList(deffered_list)
        dl.addCallback(__processProbes)
        return dl

    else:                                                               # I'm a server: let's upload
        logging.getLogger().debug("scheduler %s: coh #%s become local proxy server" % (SchedulerConfig().name, myCommandOnHostID))
        return 'server'

def getClientUsageForProxy(proxyCommandOnHostID):
    # count the (current number, max number) of clients using this proxy
    # a client is using a proxy if:
    # - getUsedProxy == proxyCommandOnHostID
    # current_state == upload_in_progress
    # to save some time, iteration is done as usual (on command from coh)
    (myCoH, myC, myT) = gatherCoHStuff(proxyCommandOnHostID)
    if myCoH == None: # current_client_number == max_client_number => dont use this target as a possible proxy
        return (0, 0)
    session = sqlalchemy.orm.create_session()
    database = MscDatabase()
    client_count = session.query(CommandsOnHost).\
        select_from(database.commands_on_host.join(database.commands).join(database.target)).\
        filter(database.commands.c.id == myC.id).\
        filter(database.commands_on_host.c.fk_use_as_proxy == myCoH.getId()).\
        filter(database.commands_on_host.c.current_state == 'upload_in_progress').\
        count()
    session.close()
    return (client_count, myCoH.getMaxClientsPerProxy())

def getProxyModeForCommand(myCommandOnHostID):
    # Preliminar iteration to gather information about this command
    # the idea being to obtain some informations about what's going on
    # we are looking for the following elements
    # - the amount of priorities:
    #   + only one => split mode (returns "split")
    #   + as many as proxies => queue mode (returns "queue")
    #   + no / not enough priorities => error condition (returns False)

    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return False

    spotted_priorities = dict()

    session = sqlalchemy.orm.create_session()
    database = MscDatabase()
    for q in session.query(CommandsOnHost).\
        select_from(database.commands_on_host.join(database.commands).join(database.target)).\
        filter(database.commands.c.id == myC.id).\
        all():
            if q.order_in_proxy != None: # some potential proxy
                if q.order_in_proxy in spotted_priorities:
                    spotted_priorities[q.order_in_proxy] += 1
                else:
                    spotted_priorities[q.order_in_proxy] = 1
    session.close()

    if len(spotted_priorities) == 0:
        return False
    elif len(spotted_priorities) == 1: # only one priority for all => split mode
        logging.getLogger().debug("scheduler %s: command #%s is in split proxy mode" % (SchedulerConfig().name, myC.id))
        return 'split'
    elif len(spotted_priorities) == reduce(lambda x, y: x+y, spotted_priorities.values()): # one priority per proxy => queue mode
        logging.getLogger().debug("scheduler %s: command #%s is in queue proxy mode" % (SchedulerConfig().name, myC.id))
        return 'queue'
    else: # other combinations are errors
        logging.getLogger().debug("scheduler %s: can'f guess proxy mode for command #%s" % (SchedulerConfig().name, myC.id))
        return False

def localProxyMayContinue(myCommandOnHostID):
    """ attempt to analyse coh in the same command in order to now how we may advance.
    """
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return False # TODO : when an error occur, do we want to clean the local proxy ?

    # Clean algorithm:
    # client => always cleanup
    # server => cleanup only if *everybody" are in one of the following state:
    #   - upload done
    #   - upload ignored
    #   - failed
    #   - over_timed
    # to prevent race condition, not check is perform to count only our clients but everybody client

    if myCoH.isLocalProxy(): # proxy server, way for clients to be done
        logging.getLogger().debug("scheduler %s: checking if we may continue coh #%s" % (SchedulerConfig().name, myCommandOnHostID))
        our_client_count = 0
        if myC.hasToUseQueueProxy():
            session = sqlalchemy.orm.create_session()
            database = MscDatabase()
            our_client_count = session.query(CommandsOnHost).\
                select_from(database.commands_on_host.join(database.commands).join(database.target)).\
                filter(database.commands.c.id == myC.id).\
                filter(database.commands_on_host.c.id != myCoH.getId()).\
                filter(database.commands_on_host.c.uploaded != 'DONE').\
                filter(database.commands_on_host.c.uploaded != 'IGNORED').\
                filter(database.commands_on_host.c.current_state != 'failed').\
                filter(database.commands_on_host.c.current_state != 'done').\
                filter(database.commands_on_host.c.current_state != 'over_timed').\
                count()
            logging.getLogger().debug("scheduler %s: found %s coh to be uploaded in command #%s" % (SchedulerConfig().name, our_client_count, myC.id))
            session.close()
        elif myC.hasToUseSplitProxy():
            session = sqlalchemy.orm.create_session()
            database = MscDatabase()
            our_client_count = session.query(CommandsOnHost).\
                select_from(database.commands_on_host.join(database.commands).join(database.target)).\
                filter(database.commands.c.id == myC.id).\
                filter(database.commands_on_host.c.id != myCoH.getId()).\
                filter(database.commands_on_host.c.order_in_proxy == None).\
                filter(database.commands_on_host.c.uploaded != 'DONE').\
                filter(database.commands_on_host.c.uploaded != 'IGNORED').\
                filter(database.commands_on_host.c.current_state != 'failed').\
                filter(database.commands_on_host.c.current_state != 'done').\
                filter(database.commands_on_host.c.current_state != 'over_timed').\
                count()
            logging.getLogger().debug("scheduler %s: found %s coh to be uploaded in command #%s" % (SchedulerConfig().name, our_client_count, myC.id))
            session.close()
        # proxy tracking update
        if our_client_count != 0:
            LocalProxiesUsageTracking().create_proxy(myT.getUUID(), myCoH.getMaxClientsPerProxy(), myC.getId())
            logging.getLogger().debug("scheduler %s: (re-)adding %s (#%s) to proxy pool" % (SchedulerConfig().name, myT.getUUID(), myC.getId()))
        else:
            LocalProxiesUsageTracking().delete_proxy(myT.getUUID(), myC.getId())
            logging.getLogger().debug("scheduler %s: (re-)removing %s (#%s) from proxy pool" % (SchedulerConfig().name, myT.getUUID(), myC.getId()))
        return our_client_count == 0
    else:
        return True

def startAnalyseCommands(scheduler_name):
    logger = logging.getLogger()

    return twisted.internet.threads.deferToThread(gatherIdsToAnalyse, scheduler_name).addCallback(analyseCommands)

def gatherIdsToAnalyse(scheduler_name):

    session = sqlalchemy.orm.create_session()
    database = MscDatabase()

    # gather candidates : long story short, takes everything which is are
    # preemptable ( not in PULSE2_UNPREEMPTABLE_STATES))
    # no matter other states
    commands_query = session.query(CommandsOnHost).\
        select_from(database.commands_on_host.join(database.commands)
        ).filter(sqlalchemy.not_(database.commands_on_host.c.current_state.in_(PULSE2_UNPREEMPTABLE_STATES))
        ).filter(sqlalchemy.or_(
            database.commands_on_host.c.scheduler == '',
            database.commands_on_host.c.scheduler == scheduler_name,
            database.commands_on_host.c.scheduler == None)
        )

    commands_to_analyse = []
    for q in commands_query.all():
        commands_to_analyse.append(q.id)

    session.close()
    return commands_to_analyse

def analyseCommands(commands_to_analyse):

    for id in commands_to_analyse:

        report = list() # will hold each report line
        this_is_a_weird_command = False
        (myCoH, myC, myT) = gatherCoHStuff(id)
        report.append("Command_on_host #%s" % id)

        # check stage against next stage state
        # algo is easy:
        # iterate over each(stage, stage+1) in that order,
        # and check that stage+1 is 'TODO' if stage is bloking (FAILED,
        # WORK_IN_PROGRESS) or has not been done (TODO)
        for stage_number in range(0, len(PULSE2_STAGES) - 1):
            # example: current_stage = awoken, next_stage = uploaded
            current_stage = PULSE2_STAGES[stage_number]
            next_stage = PULSE2_STAGES[stage_number + 1]
            current_stage_state = getattr(myCoH, current_stage)
            next_stage_state = getattr(myCoH, next_stage)

            if current_stage_state in [PULSE2_STAGE_TODO, PULSE2_STAGE_WORK_IN_PROGRESS, PULSE2_STAGE_FAILED] and next_stage_state not in [PULSE2_STAGE_TODO]:
                report.append("stage #1 : incoherency found : '%s' = '%s', '%s' = '%s')" % (current_stage, current_stage_state, next_stage, next_stage_state))
                this_is_a_weird_command = True

        # check current state according to prefix against stages
        # first iteration; check against _done
        for state_prefix_number in range(0, len(PULSE2_STATE_PREFIXES) - 1):
            if myCoH.current_state == "%s_done" % (PULSE2_STATE_PREFIXES[state_prefix_number]):
                # stages pre-this one (including this one, hence the "+1" below) should all be 'DONE' or 'IGNORED'
                for stage_number in range(0, state_prefix_number + 1):
                    current_stage = PULSE2_STAGES[stage_number]
                    current_stage_state = getattr(myCoH, current_stage)
                    if current_stage_state not in [PULSE2_STAGE_IGNORED, PULSE2_STAGE_DONE]:
                        report.append("stage #2 : incoherency found : current_state = '%s', stage '%s' = '%s')" % (myCoH.current_state, current_stage, current_stage_state))
                        this_is_a_weird_command = True

                # stages post-this one (not including this one, hence the "+1" below) should all be 'TODO'
                for stage_number in range(state_prefix_number + 1, len(PULSE2_STATE_PREFIXES)):
                    current_stage = PULSE2_STAGES[stage_number]
                    current_stage_state = getattr(myCoH, current_stage)
                    if current_stage_state not in [PULSE2_STAGE_TODO]:
                        report.append("stage #3 : incoherency found : current_state = '%s', stage '%s' = '%s')" % (myCoH.current_state, current_stage, current_stage_state))
                        this_is_a_weird_command = True

        # second iteration; check against _failed
        for state_prefix_number in range(0, len(PULSE2_STATE_PREFIXES) - 1):
            if myCoH.current_state == "%s_failed" % (PULSE2_STATE_PREFIXES[state_prefix_number]):
                # stages pre-this one should all be 'DONE' or 'IGNORED'
                for stage_number in range(0, state_prefix_number):
                    current_stage = PULSE2_STAGES[stage_number]
                    current_stage_state = getattr(myCoH, current_stage)
                    if current_stage_state not in [PULSE2_STAGE_IGNORED, PULSE2_STAGE_DONE]:
                        report.append("stage #4 : incoherency found : current_state = '%s', stage '%s' = '%s')" % (myCoH.current_state, current_stage, current_stage_state))
                        this_is_a_weird_command = True

                # this one should be obviously 'FAILED'
                stage_number = state_prefix_number
                current_stage = PULSE2_STAGES[stage_number]
                current_stage_state = getattr(myCoH, current_stage)
                if current_stage_state not in [PULSE2_STAGE_FAILED]:
                    report.append("stage #5 : incoherency found : current_state = '%s', stage '%s' = '%s')" % (myCoH.current_state, current_stage, current_stage_state))
                    this_is_a_weird_command = True

                # stages post-this one (not including this one, hence the "+1" below) should all be 'TODO'
                for stage_number in range(state_prefix_number + 1, len(PULSE2_STATE_PREFIXES)):
                    current_stage = PULSE2_STAGES[stage_number]
                    current_stage_state = getattr(myCoH, current_stage)
                    if current_stage_state not in [PULSE2_STAGE_TODO]:
                        report.append("stage #6 : incoherency found : current_state = '%s', stage '%s' = '%s')" % (myCoH.current_state, current_stage, current_stage_state))
                        this_is_a_weird_command = True

        if this_is_a_weird_command :
            logging.getLogger().warn('scheduler "%s": report for %s' % (SchedulerConfig().name, '\n'.join(report)))
            logging.getLogger().warn('Scheduler: Stopping command_on_host #%s' % (id))
            myCoH.setStateStopped()

def startAllCommands(scheduler_name, commandIDs = []):
    logger = logging.getLogger()

    if commandIDs:
        logger.debug('scheduler "%s": START: Starting commands %s' % (scheduler_name, commandIDs))
    else:
        logger.debug('scheduler "%s": START: Starting all commands' % scheduler_name)
    return twisted.internet.threads.deferToThread(gatherIdsToStart, scheduler_name, commandIDs).addCallback(sortCommands)

def gatherIdsToStart(scheduler_name, commandIDs = []):

    session = sqlalchemy.orm.create_session()
    database = MscDatabase()

    # gather candidates : long story short, takes everything which is not
    # beeing processed (in PULSE2_PROGRESSING_STATES))
    # unpreemptable (in PULSE2_UNPREEMPTABLE_STATES))
    # ignore tasks with no retries left
    # take tasks with next launch time in the future
    #
    # Please pay attention that as nowhere is is specified the commands start_date and end_date
    # fields 'special' values ("0000-00-00 00:00:00" and "2031-12-31 23:59:59"), I
    # consider that:
    #  - start_date:
    #   + "0000-00-00 00:00:00" means "as soon as possible",
    #   + "2031-12-31 23:59:59" means "never",
    #  - end_date:
    #   + "0000-00-00 00:00:00" means "never" (yeah, that's f*****g buggy, but how really matter the *specs*, hu ?),
    #   + "2031-12-31 23:59:59" means "never",
    #
    # consequently, I may process tasks:
    #     with start_date = "0000-00-00 00:00:00" or start_date <= now
    # and start_date <> "2031-12-31 23:59:59"
    # and end_date = "0000-00-00 00:00:00" or end_date = "2031-12-31 23:59:59" or end_date >= now
    #
    # TODO: check command state integrity AND command_on_host state integrity in a separtseparate function

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    soon = time.strftime("0000-00-00 00:00:00")
    later = time.strftime("2031-12-31 23:59:59")

    commands_query = session.query(CommandsOnHost).\
        select_from(database.commands_on_host.join(database.commands)
        ).filter(sqlalchemy.not_(database.commands_on_host.c.current_state.in_(PULSE2_PROGRESSING_STATES))
        ).filter(sqlalchemy.not_(database.commands_on_host.c.current_state.in_(PULSE2_UNPREEMPTABLE_STATES))
        ).filter(database.commands_on_host.c.attempts_left > 0
        ).filter(database.commands_on_host.c.next_launch_date <= now
        ).filter(sqlalchemy.or_(
            database.commands.c.start_date == soon,
            database.commands.c.start_date <= now)
        ).filter(database.commands.c.start_date != later
        ).filter(sqlalchemy.or_(
            database.commands.c.end_date == soon,
            database.commands.c.end_date == later,
            database.commands.c.end_date > now)
        ).filter(sqlalchemy.or_(
            database.commands_on_host.c.scheduler == '',
            database.commands_on_host.c.scheduler == scheduler_name,
            database.commands_on_host.c.scheduler == None)
        )

    if commandIDs:
        commands_query = commands_query.filter(database.commands.c.id.in_(commandIDs))

    commands_to_perform = []
    for q in commands_query.all():
        commands_to_perform.append(q.id)

    session.close()
    return commands_to_perform

def sortCommands(commands_to_perform):
    """
    Process CommandsOnHost objects list and fires needed deferred objects to
    perform the commands on background.
    """

    def _cb(result, tocome_distribution):
        deffereds = [] # will hold all deferred

        ids_list = [] # will contain the IDs from commands to run
        # list is pre-filled in case of something goes wrong below
        for ids in tocome_distribution.values():
            ids_list += ids

        if len(ids_list) == 0:
            logging.getLogger().info('scheduler "%s": START: Starting no command' % (SchedulerConfig().name))
            return deffereds

        logging.getLogger().debug("scheduler %s: START: Sorting the following commands: %s" % (SchedulerConfig().name, ids_list))
        try: # this code is not well tested: let's protect it :D
            # tocome_distribution is a dict, keys are the current group names, values are the ids (array) of commands to launch
            current_distribution = dict()
            for launcher in result:
                for group in result[launcher]['by_group']:
                    if not group in current_distribution:
                        current_distribution[group] = result[launcher]['by_group'][group]['running']
                    else:
                        current_distribution[group] += result[launcher]['by_group'][group]['running']
            # => now current_distribution is a dict, keys are the current group names, values are the used slots per group fully aggregated over all launchers

            # lets build an array with aggregated stats
            aggregated_distribution = dict()
            for key in tocome_distribution:
                if key not in aggregated_distribution:
                    aggregated_distribution[key] = {'tocome': 0, 'current': 0}
                aggregated_distribution[key]['tocome'] = len(tocome_distribution[key])
            for key in current_distribution:
                if key not in aggregated_distribution:
                    aggregated_distribution[key] = {'tocome': 0, 'current': 0}
                aggregated_distribution[key]['current'] = current_distribution[key]
            # we now got a dict, which for each group contains how mush stuff we are doing, and how much stuff we want to add

            # the next step is to know how much command we want to run
            # we got two options here:
            # - either run as many commands as possible while staying below a certain ceil per group
            # - or run as any commands as possible while keeping deployment equilibrated group per group

            # first case: we want to
            # - run as many commands as possible,
            # - at last obtain as many commands running as configured in scheduler.ini
            # thus we have reach max_slots / max group count
            to_reach = int(SchedulerConfig().max_slots / getMaxNumberOfGroups())

            # second case: we want to
            # - run as many commands as possible,
            # - at last obtain the same ammount of running command per group
            # so the idea is to find the group where tocome + current is minimum,
            # then raise all groups to this level
            # the calculs are done here, but please read carefuly:
            # !!!!! DO NOT USE THIS VALUE !!!!!
            # IT MAY PREVENT SCHEDULER TO RUN AT FULL CAPACITY IF A GROUP IS ALMOST EMPTY
            # to_reach = min(map(lambda(x,y): y['current'] + y['tocome'], aggregated_distribution.items()))

            # we can now obtain the full list of command_id
            ids_list = []
            for group in tocome_distribution.keys():
                for i in range(0, to_reach - aggregated_distribution[group]['current']): # some space left in this group
                    if aggregated_distribution[group]['tocome'] > 0:                     # and some stuff to add to this group
                        if len(tocome_distribution[group]):
                            ids_list.append(tocome_distribution[group].pop(0))
            random.shuffle(ids_list)
            logging.getLogger().debug("scheduler %s: START: Commands sorted: %s" % (SchedulerConfig().name, ids_list))
        except: # hum, something goes weird, try to get ids_list anyway
            logging.getLogger().debug("scheduler %s: START: Something goes wrong while sorting commands, keeping list untouched" % (SchedulerConfig().name))

        logging.getLogger().info('scheduler "%s": START: %d commands to start' % (SchedulerConfig().name, len(ids_list)))
        for id in ids_list:
            deffered = runCommand(id)
            if deffered:
                deffereds.append(deffered)
        return deffereds

    # build array of commands to perform
    tocome_distribution = dict()

    # a few pre-randomization to avoid dead locks
    random.shuffle(commands_to_perform)

    for command_id in commands_to_perform:
        (myCoH, myC, myT) = gatherCoHStuff(command_id)
        if myCoH == None:
            continue
        command_group = getClientGroup(myT)
        if not command_group in tocome_distribution:
            tocome_distribution[command_group] = [command_id]
        else:
            tocome_distribution[command_group].append(command_id)

    # build array of commands being processed by available launchers
    return getLaunchersBalance().\
        addCallback(_cb, tocome_distribution)

def getRunningCommandsOnHostInDB(scheduler_name, ids = None):
    # get the list of running commands according to the database content
    # ifs ids provided, returns only coh whose id corresponds
    session = sqlalchemy.orm.create_session()
    database = MscDatabase()

    query = session.query(CommandsOnHost
        ).select_from(database.commands_on_host
        ).filter(database.commands_on_host.c.current_state.in_(PULSE2_RUNNING_STATES)
        ).filter(sqlalchemy.or_(
            database.commands_on_host.c.scheduler == '',
            database.commands_on_host.c.scheduler == scheduler_name,
            database.commands_on_host.c.scheduler == None)
        )
    if type(ids) == list:
        query = query.\
            filter(database.commands_on_host.c.id.in_(ids))
    elif type(ids) == int:
        query = query.\
            filter(database.commands_on_host.c.id == ids)

    ret = map(lambda q: q.id, query.all())
    session.close()
    return ret

def getCommandsToNeutralize(scheduler_name):
    # get the list of commands which can be put into "over_timed" state,
    # ie :
    # - exhausted according to their end date
    # - and not yet terminated according to their current state
    session = sqlalchemy.orm.create_session()
    database = MscDatabase()
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    query = session.query(CommandsOnHost
        ).select_from(database.commands_on_host.join(database.commands)
        ).filter(sqlalchemy.not_(
            database.commands_on_host.c.current_state.in_(PULSE2_TERMINATED_STATES))
        ).filter(sqlalchemy.and_(
            database.commands.c.end_date <= now,
            database.commands.c.end_date != '0000-00-00 00:00:00')
        ).filter(sqlalchemy.or_(
            database.commands_on_host.c.scheduler == '',
            database.commands_on_host.c.scheduler == scheduler_name,
            database.commands_on_host.c.scheduler == None)
        )

    ret = map(lambda q: q.id, query.all())
    session.close()
    return ret

def stopElapsedCommands(scheduler_name):
    logger = logging.getLogger()
    logger.debug('scheduler "%s": STOP: Stopping all commands' % scheduler_name)
    return twisted.internet.threads.deferToThread(gatherIdsToStop, scheduler_name).addCallback(stopCommandsOnHosts)

def gatherIdsToStop(scheduler_name):
    # gather candidates to stop:
    # - build the list of running commands (according to the DB),
    # - commands not in their deployment interval get killed,
    # - commands exhausted get killed *and* tagged as over_timed
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    ids = list()
    for id in getRunningCommandsOnHostInDB(scheduler_name):
        (myCoH, myC, myT) = gatherCoHStuff(id)
        if myCoH == None:
            continue
        if not myC.inDeploymentInterval(): # stops command not in interval
            ids.append(id)
        elif myC.end_date.__str__() != '0000-00-00 00:00:00' and myC.end_date.__str__() <= now:
            myCoH.setStateOverTimed() # change the CoH current_state (we are not going to be able to try to start this coh ever again)
            ids.append(id)

    # this loop only put the current_state in over_timed, but as the coh
    # are not running, we dont need to stop them.
    # /!\ has to be run *after* previous loop
    for id in getCommandsToNeutralize(scheduler_name):
        (myCoH, myC, myT) = gatherCoHStuff(id)
        if myCoH == None:
            continue
        logging.getLogger().debug("Scheduler: Over Timed command_on_host #%s" % (id))
        myCoH.setStateOverTimed()

    return ids

def fixUnprocessedTasks(scheduler_name):
    logging.getLogger().debug('scheduler "%s": FUT: Starting analysis' % scheduler_name)
    return twisted.internet.threads.deferToThread(getRunningCommandsOnHostInDB, scheduler_name, None).addCallback(cleanStatesAllRunningIds)

def fixProcessingTasks(scheduler_name):
    logging.getLogger().debug('scheduler "%s": FPT: Starting analysis' % scheduler_name)
    # using lambda as deferToThread do not accept DeferredList
    return twisted.internet.threads.deferToThread(lambda woot: woot, scheduler_name).addCallback(getRunningCommandsOnHostFromLaunchers).addCallback(stopCommandsOnHosts)

def cleanStatesAllRunningIds(ids):
    """
        algo is pretty simple:
        - ids is the result of getRunningCommandsOnHostInDb(),
        or in other words the exact list of running commands according
        to the DB.
        - we ask each launcher to give us the list of commands
        still being processed (using xmlrpc 'get_process_ids').
        - __treatBadStateCommandsOnHost() will compare the list of ids
        given by the launchers to the list given by
        getRunningCommandsOnHostInDb.
        - each process
          + reported by getRunningCommandsOnHostInDb()
          + not reported by at least one launcher
        get tagged as "STOPPED" in the database

        FIXME: the downside of this is when a launcher is - for a short
        time - unreachable but still processing a command, this commands
        can be tagged as "STOPPED" without reason. And we do not have
        any way to tell if an unreachable launcher is down or not.
        That's why this feature is disabled by default.
    """

    def __treatBadStateCommandsOnHost(result, ids = ids):
        fails = []
        if len(ids) > 0:
            logging.getLogger().debug('scheduler "%s": FUT: Looking if the following tasks are still running  : %s' % (SchedulerConfig().name, str(ids)))
        else:
            logging.getLogger().debug('scheduler "%s": FUT: No task should be running' % (SchedulerConfig().name))
        for id in ids:
            found = False
            for running_ids in result: # one tuple per launcher
                if running_ids[1] == None: # None: launcher may be down
                    continue
                if id in running_ids[1]:
                    found = True
                    continue
            if not found:
                fails.append(id)
                logging.getLogger().warn('scheduler "%s": FUT: Forcing the following command to STOP state: %s' % (SchedulerConfig().name, id))
        for id in fails:
            (myCoH, myC, myT) = gatherCoHStuff(id)
            myCoH.setStateStopped()

    deffereds = [] # will hold all deferred
    for launcher in SchedulerConfig().launchers_uri.values():
        # we want all the commands beeing treated by the launcher
        deffered = callOnLauncher(None, launcher, 'get_process_ids')
        if deffered:
            deffereds.append(deffered)

    deffered_list = twisted.internet.defer.DeferredList(deffereds)
    deffered_list.addCallbacks(
        __treatBadStateCommandsOnHost,
        lambda reason: logging.getLogger().error('scheduler "%s": FUT: error %s'  % (SchedulerConfig().name, reason.value))
    )
    return deffered_list

def getRunningCommandsOnHostFromLaunchers(scheduler_name):
    """
        algo is pretty simple:
        - ids is the result of getRunningCommandsOnHostFromLaunchers(),
        or in other words the exact list of running commands according
        to our launchers
        - we ask the database to give us the list of commands
        still being processed (using getRunningCommandsOnHostInDB()).
        - __treatRunningCommandsOnHostFromLaunchers() will compare the
        list of ids given by the launchers to the list given by
        getRunningCommandsOnHostInDb.
        - each process
          + not reported by getRunningCommandsOnHostInDb()
          + reported by at least one launcher
        is asked to be stopped.

        FIXME: as they are really not reason for a process to still be run
        by a launcher and being tagged as not running in database,
        this feature is disabled by default.
    """

    def __treatRunningCommandsOnHostFromLaunchers(result, scheduler_name=scheduler_name):
        # check if they should be running (database)
        launchers_running_ids = []
        for running_ids in result:
            if running_ids[1] != None: # None: launcher may be down
                for id in running_ids[1]:
                    launchers_running_ids.append(id)
        if len(launchers_running_ids) > 0:
            logging.getLogger().debug('scheduler "%s": FPT: Launchers are running coh %s'  % (SchedulerConfig().name, str(launchers_running_ids)))
        else:
            logging.getLogger().debug('scheduler "%s": FPT: Launchers are not running any coh'  % (SchedulerConfig().name))

        # get the states of launchers_running_ids
        fails = []
        for id in launchers_running_ids:
            if id not in getRunningCommandsOnHostInDB(scheduler_name, launchers_running_ids):
                fails.append(id)
        if len(fails) > 0:
            logging.getLogger().info('scheduler "%s": FPT: Forcing the following commands to STOP : %s' % (SchedulerConfig().name, str(fails)))
            # start stopping commands on launcher
        return fails

    deffereds = [] # will hold all deferred
    for launcher in SchedulerConfig().launchers_uri.values():
        # we only want the commands beeing executed right now
        deffered = callOnLauncher(None, launcher, 'get_running_ids')
        if deffered:
            deffereds.append(deffered)

    deffered_list = twisted.internet.defer.DeferredList(deffereds)
    deffered_list.addCallbacks(
        __treatRunningCommandsOnHostFromLaunchers,
        lambda reason: logging.getLogger().error('scheduler "%s": FPT: error %s'  % (SchedulerConfig().name, reason.value))
    )
    return deffered_list

def stopCommandsOnHosts(ids):
    deffereds = [] # will hold all deferred
    if len(ids) > 0:
        logging.getLogger().info('scheduler "%s": STOP: %d commands to stop' % (SchedulerConfig().name, len(ids)))
        for launcher in SchedulerConfig().launchers_uri.values():
            deffered = callOnLauncher(None, launcher, 'term_processes', ids)
            if deffered:
                deffereds.append(deffered)
    else:
        logging.getLogger().info('scheduler "%s": STOP: Stopping no command' % (SchedulerConfig().name))
    return deffereds

def stopCommand(myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return False
    logger = logging.getLogger()
    logger.info("going to terminate command_on_host #%s from command #%s" % (myCoH.getId(), myCoH.getIdCommand()))
    logger.debug("command_on_host state is %s" % myCoH.toH())
    logger.debug("command state is %s" % myC.toH())
    for launcher in SchedulerConfig().launchers_uri.values():
        callOnLauncher(None, launcher, 'term_process', myCommandOnHostID)
    return True

def startCommand(myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return False
    logger = logging.getLogger()
    if myCoH.scheduler not in [SchedulerConfig().name, '', None]:
        logger.warn("attempt to start command_on_host #%s from command #%s using the wrong scheduler" % (myCoH.getId(), myCoH.getIdCommand()))
        return False

    logger.info("Starting command_on_host #%s from command #%s" % (myCoH.getId(), myCoH.getIdCommand()))
    runCommand(myCommandOnHostID)
    return True

def startTheseCommands(scheduler_name, commandIDs):
    """
    Tell the scheduler to immediately start a given command
    """
    return startAllCommands(scheduler_name, commandIDs)

def runCommand(myCommandOnHostID):
    """
        Just a simple start point, chain-load on Upload Phase
    """
    if checkAndFixCommand(myCommandOnHostID):
        if SchedulerConfig().lock_processed_commands and not CommandsOnHostTracking().preempt(myCommandOnHostID): return
        return runWOLPhase(myCommandOnHostID)
    else:
        logging.getLogger().warn("NOT going to start command_on_host #%s, check failed" % (myCommandOnHostID))

def checkAndFixCommand(myCommandOnHostID):
    """
        pass command through a filter, trying to guess is command is valid
        four cases here:
         - command IS valid, return True
         - command is ALMOST valid, fix it, return True
         - command is a little invalid, neutralize it, return False
         - command IS completely messed-up, return False
    """
    logger = logging.getLogger()

    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)

    # a simple database checkup
    if myCoH == None:
        return False

    # give up if target can't be retrieved
    if not myT.hasEnoughInfoToConnect():
        myCoH.setStateFailed()
        updateHistory(myCommandOnHostID, None, PULSE2_TARGET_NOTENOUGHINFO_ERROR, '', 'Not enough information to establish a connection') # FIXME : error classe is wrong !
        return False

    # give up if command not in interval
    if not myC.inDeploymentInterval():
        return False

    if myC.isPartOfABundle():
        logger.debug("command_on_host #%s: part of bundle %s, order %s " % (myCoH.getId(), myC.getBundleId(), myC.getOrderInBundle()))
        deps_status = getDependancies(myCommandOnHostID)
        # give up if bundle and can't guess dependancies
        if type(deps_status) == bool and not deps_status:
            logger.debug("command_on_host #%s: failed to get dependencies" % (myCoH.getId()))
            return False
        # give up if bundle and some dependancies remain
        if deps_status == "dead":
            myCoH.setStateFailed()
            updateHistory(myCommandOnHostID, None, PULSE2_BUNDLE_MISSING_MANDATORY_ERROR, '', 'Bundle stopped since a mandatory part could not be done') # FIXME : error classe is wrong !
            return False # give up, some deps have failed
        if deps_status == "wait":
            return False # wait, some deps has to be done
        if deps_status != "run":
            return False # weird, should not append

    logger.debug("going to do command_on_host #%s from command #%s" % (myCoH.getId(), myCoH.getIdCommand()))
    logger.debug("command_on_host state is %s" % myCoH.toH())
    logger.debug("command state is %s" % myC.toH())
    myCoH.setStartDate()
    return True

def runWOLPhase(myCommandOnHostID):
    """
        Attempt do see if a wake-on-lan should be done
    """
    def _cb(result):
        """ results
            0 => ping NOK => do WOL
            1 => ping OK, ssh NOK  => do WOL (computer may just have awoken)
            2 => ping OK, ssh OK => don't do WOL
        """
        if result == 2:
            logger.info("command_on_host #%s: do not wol (target already up)" % myCommandOnHostID)
            # FIXME: state will be 'wol_ignored' when implemented in database
            updateHistory(myCommandOnHostID, None, PULSE2_SUCCESS_ERROR, "skipped: host already up", "")
            myCoH.setWOLIgnored()
            myCoH.setStateScheduled()
            return runUploadPhase(myCommandOnHostID)
        logger.info("command_on_host #%s: do wol (target not up)" % myCommandOnHostID)
        return performWOLPhase(myCommandOnHostID)

    def _eb(reason):
        logger.warn("command_on_host #%s: while probing: %s" % (myCommandOnHostID, reason))
        logger.info("command_on_host #%s: do wol (target not up)" % myCommandOnHostID)
        return performWOLPhase(myCommandOnHostID)

    # check for WOL condition in order to give up if needed
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)

    logger = logging.getLogger()
    logger.info("command_on_host #%s: WOL phase" % myCommandOnHostID)

    if myCoH.isWOLRunning():            # WOL in progress
        if myCoH.getLastWOLAttempt() != None: # WOL *really* progress, hem
            if (datetime.datetime.now()-myCoH.getLastWOLAttempt()).seconds < (SchedulerConfig().max_wol_time + 300):
                # we should wait a little more
                return runGiveUpPhase(myCommandOnHostID)
            else:
                # we already pass the delay from at least 300 seconds, let's continue
                # FIXME: dirty fix, better use a sem system to handle collision situations :/
                logging.getLogger().warn("command_on_host #%s: WOL should have been set as done !" % (myCommandOnHostID))
                myCoH.setWOLDone()
                myCoH.setStateScheduled()
                return runUploadPhase(myCommandOnHostID)
        else: # WOL marked as "in progress", but no time given ?!
            # return None to avoid some possible race conditions
            return runGiveUpPhase(myCommandOnHostID)

        logger.info("command_on_host #%s: WOL still running" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

    if myCoH.isWOLIgnored(): # wol has already been ignored, jump to next stage
        logger.info("command_on_host #%s: wol ignored" % myCoH.getId())
        return runUploadPhase(myCommandOnHostID)
    if myCoH.isWOLDone(): # wol has already already done, jump to next stage
        logger.info("command_on_host #%s: wol done" % myCoH.getId())
        return runUploadPhase(myCommandOnHostID)
    if not myCoH.isWOLImminent():       # nothing to do right now, give out
        logger.info("command_on_host #%s: not the right time to WOL" % myCoH.getId())
        return runGiveUpPhase(myCommandOnHostID)
    if not myC.hasToWOL(): # don't have to WOL
        logger.info("command_on_host #%s: do not wol" % myCoH.getId())
        myCoH.setWOLIgnored()
        myCoH.setStateScheduled()
        return runUploadPhase(myCommandOnHostID)
    if not myT.hasEnoughInfoToWOL(): # not enough information to perform WOL: ignoring phase but writting this in DB
        logger.warn("command_on_host #%s: wol couldn't be performed; not enough information in target table" % myCoH.getId())
        # FIXME: state will be 'wol_ignored' when implemented in database
        updateHistory(myCommandOnHostID, None, PULSE2_TARGET_NOTENOUGHINFO_ERROR, " skipped : not enough information in target table")
        myCoH.setWOLIgnored()
        myCoH.setStateScheduled()
        return runUploadPhase(myCommandOnHostID)

    # WOL has to be performed, but only if computer is down (ie. no ping)

    # update command state
    myCoH.setWOLInProgress()
    myCoH.setStateWOLInProgress()
    myCoH.setLastWOLAttempt()
    updateHistory(myCommandOnHostID, 'wol_in_progress')

    uuid = myT.target_uuid
    fqdn = myT.target_name
    shortname = myT.target_name
    ips = myT.target_ipaddr.split('||')
    macs = myT.target_macaddr.split('||')
    mydeffered = pingAndProbeClient(uuid, fqdn, shortname, ips, macs)
    mydeffered.\
        addCallback(_cb).\
        addErrback(_eb)
    return mydeffered

def performWOLPhase(myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)

    # perform call
    mydeffered = callOnBestLauncher(myCommandOnHostID,
        'wol',
        False,
        myT.target_macaddr.split('||'),
        myT.target_bcast.split('||')
    )

    mydeffered.\
        addCallback(parseWOLAttempt, myCommandOnHostID).\
        addErrback(parseWOLError, myCommandOnHostID)
    return mydeffered

def runUploadPhase(myCommandOnHostID):
    """
        Handle first Phase: upload time
    """
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().info("command_on_host #%s: copy phase" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)

    # check for upload condition in order to give up if needed
    if myCoH.isUploadRunning(): # upload still running, immediately returns
        logging.getLogger().info("command_on_host #%s: still running" % myCoH.getId())
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isUploadIgnored(): # upload has already been ignored, jump to next stage
        logging.getLogger().info("command_on_host #%s: upload ignored" % myCoH.getId())
        return runExecutionPhase(myCoH.getId())
    if myCoH.isUploadDone(): # upload has already already done, jump to next stage
        logging.getLogger().info("command_on_host #%s: upload done" % myCoH.getId())
        return runExecutionPhase(myCoH.getId())
    if not myCoH.isUploadImminent(): # nothing to do right now, give out
        logging.getLogger().info("command_on_host #%s: nothing to upload right now" % myCoH.getId())
        return runGiveUpPhase(myCommandOnHostID)
    if not myC.hasSomethingToUpload(): # nothing to upload here, jump to next stage
        logging.getLogger().info("command_on_host #%s: nothing to upload" % myCoH.getId())
        myCoH.setUploadIgnored()
        myCoH.setStateScheduled()
        return runExecutionPhase(myCoH.getId())

    # if we are here, upload has either previously failed or never be done
    # do copy here

    # update command state
    myCoH.setUploadInProgress()
    myCoH.setStateUploadInProgress()

    # fullfil used proxy (if we can)
    if myC.hasToUseProxy():
        d = twisted.internet.defer.maybeDeferred(localProxyUploadStatus, myCommandOnHostID)
        d.addCallback(_cbChooseUploadMode, myCoH, myC, myT)
        return d

    return _chooseUploadMode(myCoH, myC, myT)

def _cbChooseUploadMode(result, myCoH, myC, myT):
    if result == 'waiting':
        logging.getLogger().info("command_on_host #%s: waiting for a local proxy" % myCoH.getId())
        myCoH.setUploadToDo()
        myCoH.setStateScheduled()
        return runGiveUpPhase(myCoH.getId())
    elif result == 'dead':
        logging.getLogger().warn("command_on_host #%s: waiting for a local proxy which will never be ready !" % myCoH.getId())
        updateHistory(myCoH.getId(), 'upload_failed', PULSE2_PROXY_WAITINGFORDEAD_ERROR, '', 'Waiting for a local proxy which will never be ready')
        if not myCoH.switchToUploadFailed(myC.getNextConnectionDelay(), True): # better decrement attemps; proxy seems dead
            return runFailedPhase(myCoH.getId())
        return runGiveUpPhase(myCoH.getId())
    elif result == 'server':
        logging.getLogger().info("command_on_host #%s: becoming local proxy server" % myCoH.getId())
        myCoH.setUsedProxy(myCoH.getId()) # special case: this way we know we were server
    elif result == 'keeping':
        logging.getLogger().info("command_on_host #%s: keeping previously acquiered local proxy settings" % myCoH.getId())
    else:
        logging.getLogger().info("command_on_host #%s: becoming local proxy client" % myCoH.getId())
        myCoH.setUsedProxy(result)
    return _chooseUploadMode(myCoH, myC, myT)

def _chooseUploadMode(myCoH, myC, myT):
    logger = logging.getLogger()
    # check if we have enough informations to reach the client
    client = { 'host': chooseClientIP(myT), 'uuid': myT.getUUID(), 'maxbw': myC.maxbw, 'client_check': getClientCheck(myT), 'server_check': getServerCheck(myT), 'action': getAnnounceCheck('transfert'), 'group': getClientGroup(myT)}
    if not client['host']: # We couldn't get an IP address for the target host
        return twisted.internet.defer.fail(Exception("Not enough information about client to perform upload")).addErrback(parsePushError, myCoH.getId(), decrement_attempts_left = True, error_code = PULSE2_TARGET_NOTENOUGHINFO_ERROR)

    # first attempt to guess is mirror is local (push) or remove (pull) or through a proxy
    if myCoH.isProxyClient(): # proxy client
        d = _runProxyClientPhase(client, myC, myCoH)
    elif re.compile('^file://').match(myT.mirrors): # local mirror starts by "file://" : prepare a remote_push
        d = _runPushPhase(client, myC, myCoH, myT)
    else: # remote push/pull

        try: # mirror is formated like this: https://localhost:9990/mirror1||https://localhost:9990/mirror1
            mirrors = myT.mirrors.split('||')
        except:
            logger.warn("command_on_host #%s: target.mirror do not seems to be as expected, got '%s', skipping command" % (myCoH.getId(), myT.mirrors))
            return twisted.internet.defer.fail(Exception("Mirror uri %s is not well-formed" % myT.mirrors)).addErrback(parsePushError, myCoH.getId(), decrement_attempts_left = True)

        # Check mirrors
        if len(mirrors) != 2:
            logger.warn("command_on_host #%s: we need two mirrors ! '%s'" % (myCoH.getId(), myT.mirrors))
            return twisted.internet.defer.fail(Exception("Mirror uri %s do not contains two mirrors" % myT.mirrors)).addErrback(parsePushError, myCoH.getId(), decrement_attempts_left = True)
        mirror = mirrors[0]
        fbmirror = mirrors[1]

        ma = pulse2.apis.clients.mirror.Mirror(mirror)
        d = ma.isAvailable(myC.package_id)
        d.addCallback(_cbRunPushPullPhaseTestMainMirror, mirror, fbmirror, client, myC, myCoH)

    return d

def _cbRunPushPullPhaseTestMainMirror(result, mirror, fbmirror, client, myC, myCoH):
    if result:
        return _runPushPullPhase(mirror, fbmirror, client, myC, myCoH)
    else:
        # Test the fallback mirror
        return _cbRunPushPullPhaseTestFallbackMirror(result, mirror, fbmirror, client, myC, myCoH)

def _cbRunPushPullPhaseTestFallbackMirror(result, mirror, fbmirror, client, myC, myCoH):
    if fbmirror != mirror:
        # Test the fallback mirror only if the URL is the different than the
        # primary mirror
        ma = pulse2.apis.clients.mirror.Mirror(fbmirror)
        d = ma.isAvailable(myC.package_id)
        d.addCallback(_cbRunPushPullPhase, mirror, fbmirror, client, myC, myCoH, True)
        return d
    else:
        # Go to upload phase, but pass False to tell that the package is not
        # available on the fallback mirror too
        _cbRunPushPullPhase(False, mirror, fbmirror, client, myC, myCoH)

def _cbRunPushPullPhase(result, mirror, fbmirror, client, myC, myCoH, useFallback = False):
    if result:
        # The package is available on a mirror, start upload phase
        return _runPushPullPhase(mirror, fbmirror, client, myC, myCoH, useFallback)
    else:
        logging.getLogger().warn("command_on_host #%s: Package '%s' is not available on any mirror" % (myCoH.getId(), myC.package_id))
        updateHistory(myCoH.getId(), 'upload_failed', PULSE2_PSERVER_PACKAGEISUNAVAILABLE_ERROR, '', 'Package \'%s\' is not available on any mirror' % (myC.package_id))
        if not myCoH.switchToUploadFailed(myC.getNextConnectionDelay(), True): # better decrement attemps, as package can't be found
            return runFailedPhase(myCoH.getId())
        return runGiveUpPhase(myCoH.getId())

def _runProxyClientPhase(client, myC, myCoH):
    # fulfill protocol
    client['protocol'] = 'rsyncproxy'

    # get informations about our proxy
    (proxyCoH, proxyC, proxyT) = gatherCoHStuff(myCoH.getUsedProxy())
    if proxyCoH == None:
        return twisted.internet.defer.fail(Exception("Cant access to CoH")).addErrback(parsePushError, myCoH.getId(), decrement_attempts_left = True)
    proxy = { 'host': chooseClientIP(proxyT), 'uuid': proxyT.getUUID(), 'maxbw': proxyC.maxbw, 'client_check': getClientCheck(proxyT), 'server_check': getServerCheck(proxyT), 'action': getAnnounceCheck('transfert'), 'group': getClientGroup(proxyT)}
    if not proxy['host']: # We couldn't get an IP address for the target host
        return twisted.internet.defer.fail(Exception("Can't get proxy IP address")).addErrback(parsePushError, myCoH.getId(), decrement_attempts_left = True)
    # and fill struct
    # only proxy['host'] used until now
    client['proxy'] = {
        'command_id': myCoH.getUsedProxy(),
        'host': proxy['host'],
        'uuid': proxy['uuid']
    }

    # build file list
    files_list = []
    for file in myC.files.split("\n"):
        fname = file.split('##')[1]
        if re.compile('^/').search(fname):
            fname = re.compile('^/[^/]*/(.*)$').search(fname).group(1) # keeps last compontent of path
        files_list.append(fname)

    # prepare deffereds
    if SchedulerConfig().mode == 'sync':
        updateHistory(myCoH.getId(), 'upload_in_progress')
        mydeffered = callOnBestLauncher(
            myCoH.getId(),
            'sync_remote_pull',
            False,
            myCoH.getId(),
            client,
            files_list,
            SchedulerConfig().max_upload_time
        )
        mydeffered.\
            addCallback(parsePushResult, myCoH.getId()).\
            addErrback(parsePushError, myCoH.getId())
    elif SchedulerConfig().mode == 'async':
        # 'server_check': {'IP': '192.168.0.16', 'MAC': 'abbcd'}
        mydeffered = callOnBestLauncher(
            myCoH.getId(),
            'async_remote_pull',
            False,
            myCoH.getId(),
            client,
            files_list,
            SchedulerConfig().max_upload_time
        )
        mydeffered.\
            addCallback(parsePushOrder, myCoH.getId()).\
            addErrback(parsePushError, myCoH.getId())
    else:
        mydeffered = None

    return mydeffered

def _runPushPhase(client, myC, myCoH, myT):
    # fulfill protocol
    client['protocol'] = 'rsyncssh'

    # build file list
    files_list = list()
    for file in myC.files.split("\n"):
        fname = file.split('##')[1]
        if re.compile('^/').search(fname):
            fname = re.compile('^/(.*)$').search(fname).group(1)
        files_list.append(os.path.join(re.compile('^file://(.*)$').search(myT.mirrors).group(1), fname)) # get folder on mirror

    # prepare deffereds
    if SchedulerConfig().mode == 'sync':
        updateHistory(myCoH.getId(), 'upload_in_progress')
        mydeffered = callOnBestLauncher(
            myCoH.getId(),
            'sync_remote_push',
            False,
            myCoH.getId(),
            client,
            files_list,
            SchedulerConfig().max_upload_time
        )
        mydeffered.\
            addCallback(parsePushResult, myCoH.getId()).\
            addErrback(parsePushError, myCoH.getId())
    elif SchedulerConfig().mode == 'async':
        # 'server_check': {'IP': '192.168.0.16', 'MAC': 'abbcd'}
        mydeffered = callOnBestLauncher(
            myCoH.getId(),
            'async_remote_push',
            False,
            myCoH.getId(),
            client,
            files_list,
            SchedulerConfig().max_upload_time
        )
        mydeffered.\
            addCallback(parsePushOrder, myCoH.getId()).\
            addErrback(parsePushError, myCoH.getId())
    else:
        mydeffered = None

    # run deffereds
    return mydeffered

def _runPushPullPhase(mirror, fbmirror, client, myC, myCoH, useFallback = False):
    if useFallback:
        msg = 'Package \'%s\' is NOT available on primary mirror %s\nPackage \'%s\' is available on fallback mirror %s' % (myC.package_id, mirror, myC.package_id, fbmirror)
        mirror = fbmirror
    else:
        msg = 'Package \'%s\' is available on primary mirror %s' % (myC.package_id, mirror)
    updateHistory(myCoH.getId(), 'upload_in_progress', PULSE2_SUCCESS_ERROR, '', msg)
    logging.getLogger().debug("command_on_host #%s: Package '%s' is available on %s" % (myCoH.getId(), myC.package_id, mirror))
    ma = pulse2.apis.clients.mirror.Mirror(mirror)
    fids = []
    for line in myC.files.split("\n"):
        fids.append(line.split('##')[0])
    d = ma.getFilesURI(fids)
    d.addCallback(_cbRunPushPullPhasePushPull, mirror, fbmirror, client, myC, myCoH, useFallback)

def _cbRunPushPullPhasePushPull(result, mirror, fbmirror, client, myC, myCoH, useFallback):
    files_list = result
    file_uris = {}
    choosen_mirror = mirror
    if not False in files_list and not '' in files_list:
        # build a dict with the protocol and the files uris
        if re.compile('^http://').match(choosen_mirror) or re.compile('^https://').match(choosen_mirror): # HTTP download
            file_uris = {'protocol': 'wget', 'files': files_list}
        elif re.compile('^smb://').match(choosen_mirror): # TODO: NET download
            pass
        elif re.compile('^ftp://').match(choosen_mirror): # FIXME: check that wget may handle FTP as HTTP
            file_uris = {'protocol': 'wget', 'files': files_list}
        elif re.compile('^nfs://').match(choosen_mirror): # TODO: NFS download
            pass
        elif re.compile('^ssh://').match(choosen_mirror): # TODO: SSH download
            pass
        elif re.compile('^rsync://').match(choosen_mirror): # TODO: RSYNC download
            pass
        else: # do nothing
            pass

    # from here, either file_uris is a dict with a bunch of uris, or it is void in which case we give up
    if not file_uris:
        if useFallback:
            logging.getLogger().warn("command_on_host #%s: can't get files URI from fallback mirror, skipping command" % (myCoH.getId()))
            updateHistory(myCoH.getId(), 'upload_failed', PULSE2_PSERVER_GETFILEURIFROMPACKAGE_ERROR, '', \
                    'Can\'t get files URI for package \'%s\' on fallback mirror %s.\nPlease check that the package and its files have not been modified since the planification of the command.' % (myC.package_id, fbmirror))
            # the getFilesURI call failed on the fallback. We have a serious
            # problem and we better decrement attempts
            if not myCoH.switchToUploadFailed(myC.getNextConnectionDelay(), True):
                return runFailedPhase(myCoH.getId())
        elif not fbmirror or fbmirror == mirror:
            logging.getLogger().warn("command_on_host #%s: can't get files URI from mirror %s, and not fallback mirror to try" % (myCoH.getId(), mirror))
            updateHistory(myCoH.getId(), 'upload_failed', PULSE2_PSERVER_GETFILEURIFROMPACKAGE_ERROR, '', \
                    'Can\'t get files URI for package \'%s\' on mirror %s.\nPlease check that the package and its files have not been modified since the planification of the command.' % (myC.package_id, mirror))
            # the getFilesURI call failed on the only mirror we have. We have a serious
            # problem and we better decrement attempts
            if not myCoH.switchToUploadFailed(myC.getNextConnectionDelay(), True):
                return runFailedPhase(myCoH.getId())
        else:
            # Use the fallback mirror
            logging.getLogger().warn("command_on_host #%s: can't get files URI from mirror %s, trying with fallback mirror %s" % (myCoH.getId(), mirror, fbmirror))
            _cbRunPushPullPhaseTestFallbackMirror(None, mirror, fbmirror, client, myC, myCoH)
        return

    client['protocol'] = file_uris['protocol']
    files_list = file_uris['files']

    # upload starts here
    if SchedulerConfig().mode == 'sync':
        updateHistory(myCoH.getId(), 'upload_in_progress')
        mydeffered = callOnBestLauncher(
            myCoH.getId(),
            'sync_remote_pull',
            False,
            myCoH.getId(),
            client,
            files_list,
            SchedulerConfig().max_upload_time
        )
        mydeffered.\
            addCallback(parsePullResult, myCoH.getId()).\
            addErrback(parsePullError, myCoH.getId())
    elif SchedulerConfig().mode == 'async':
        mydeffered = callOnBestLauncher(
            myCoH.getId(),
            'async_remote_pull',
            False,
            myCoH.getId(),
            client,
            files_list,
            SchedulerConfig().max_upload_time
        )
        mydeffered.\
            addCallback(parsePullOrder, myCoH.getId()).\
            addErrback(parsePullError, myCoH.getId())
    else:
        return runGiveUpPhase(myCoH.getId())
    return mydeffered

def runExecutionPhase(myCommandOnHostID):
    # Second step : execute file
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logger = logging.getLogger()
    logger.info("command_on_host #%s: execution phase" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isExecutionRunning(): # execution still running, immediately returns
        logger.info("command_on_host #%s: still running" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isExecutionDone(): # execution has already been done, jump to next stage
        logger.info("command_on_host #%s: execution done" % myCommandOnHostID)
        return runDeletePhase(myCommandOnHostID)
    if myCoH.isExecutionIgnored(): # execution has already been ignored, jump to next stage
        logger.info("command_on_host #%s: execution ignored" % myCommandOnHostID)
        return runDeletePhase(myCommandOnHostID)
    if not myCoH.isExecutionImminent(): # nothing to do right now, give out
        logger.info("command_on_host #%s: nothing to execute right now" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    if not myC.hasSomethingToExecute(): # nothing to execute here, jump to next stage
        logger.info("command_on_host #%s: nothing to execute" % myCommandOnHostID)
        myCoH.setExecutionIgnored()
        myCoH.setStateScheduled()
        return runDeletePhase(myCommandOnHostID)

    if myC.hasToUseProxy():
        if not localProxyMayContinue(myCommandOnHostID):
            logger.info("command_on_host #%s: execution postponed, waiting for some clients" % myCommandOnHostID)
            myCoH.setStateScheduled()
            return runGiveUpPhase(myCommandOnHostID)

    # if we are here, execution has either previously failed or never be done
    client = { 'host': chooseClientIP(myT), 'uuid': myT.getUUID(), 'maxbw': myC.maxbw, 'protocol': 'ssh', 'client_check': getClientCheck(myT), 'server_check': getServerCheck(myT), 'action': getAnnounceCheck('execute'), 'group': getClientGroup(myT)}
    if not client['host']: # We couldn't get an IP address for the target host
        return twisted.internet.defer.fail(Exception("Not enough information about client to perform execution")).addErrback(parseExecutionError, myCommandOnHostID, decrement_attempts_left = True, error_code = PULSE2_TARGET_NOTENOUGHINFO_ERROR)

    if myC.isQuickAction(): # should be a standard script
        myCoH.setExecutionInProgress()
        myCoH.setStateExecutionInProgress()
        if SchedulerConfig().mode == 'sync':
            updateHistory(myCommandOnHostID, 'execution_in_progress')
            mydeffered = callOnBestLauncher(
                myCommandOnHostID,
                'sync_remote_quickaction',
                False,
                myCommandOnHostID,
                client,
                ' '.join([myC.start_file, myC.parameters]).strip(),
                SchedulerConfig().max_command_time
            )
            mydeffered.\
                addCallback(parseExecutionResult, myCommandOnHostID).\
                addErrback(parseExecutionError, myCommandOnHostID)
        elif SchedulerConfig().mode == 'async':
            mydeffered = callOnBestLauncher(
                myCommandOnHostID,
                'async_remote_quickaction',
                False,
                myCommandOnHostID,
                client,
                ' '.join([myC.start_file, myC.parameters]).strip(),
                SchedulerConfig().max_command_time
            )
            mydeffered.\
                addCallback(parseExecutionOrder, myCommandOnHostID).\
                addErrback(parseExecutionError, myCommandOnHostID)
        else:
            return runGiveUpPhase(myCommandOnHostID)
        return mydeffered
    else:
        myCoH.setExecutionInProgress()
        myCoH.setStateExecutionInProgress()
        if SchedulerConfig().mode == 'sync':
            updateHistory(myCommandOnHostID, 'execution_in_progress')
            mydeffered = callOnBestLauncher(
                myCommandOnHostID,
                'sync_remote_exec',
                False,
                myCommandOnHostID,
                client,
                ' '.join([myC.start_file, myC.parameters]).strip(),
                SchedulerConfig().max_command_time
            )
            mydeffered.\
                addCallback(parseExecutionResult, myCommandOnHostID).\
                addErrback(parseExecutionError, myCommandOnHostID)
        elif SchedulerConfig().mode == 'async':
            mydeffered = callOnBestLauncher(
                myCommandOnHostID,
                'async_remote_exec',
                False,
                myCommandOnHostID,
                client,
                ' '.join([myC.start_file, myC.parameters]).strip(),
                SchedulerConfig().max_command_time
            )
            mydeffered.\
                addCallback(parseExecutionOrder, myCommandOnHostID).\
                addErrback(parseExecutionError, myCommandOnHostID)
        else:
            return runGiveUpPhase(myCommandOnHostID)
        return mydeffered

def runDeletePhase(myCommandOnHostID):
    # Third step : delete file
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logger = logging.getLogger()
    logger.info("command_on_host #%s: delete phase" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isDeleteRunning(): # delete still running, immediately returns
        logging.getLogger().info("command_on_host #%s: still deleting" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isDeleteDone(): # delete has already be done, jump to next stage
        logger.info("command_on_host #%s: delete done" % myCommandOnHostID)
        return runInventoryPhase(myCommandOnHostID)
    if myCoH.isDeleteIgnored(): # delete has already be ignored, jump to next stage
        logger.info("command_on_host #%s: delete ignored" % myCommandOnHostID)
        return runInventoryPhase(myCommandOnHostID)
    if not myCoH.isDeleteImminent(): # nothing to do right now, give out
        logger.info("command_on_host #%s: nothing to delete right now" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    if not myC.hasSomethingToDelete(): # nothing to delete here, jump to next stage
        logger.info("command_on_host #%s: nothing to delete" % myCommandOnHostID)
        myCoH.setDeleteIgnored()
        myCoH.setStateScheduled()
        return runInventoryPhase(myCommandOnHostID)

    client = { 'host': chooseClientIP(myT), 'uuid': myT.getUUID(), 'maxbw': myC.maxbw, 'protocol': 'ssh', 'client_check': getClientCheck(myT), 'server_check': getServerCheck(myT), 'action': getAnnounceCheck('delete'), 'group': getClientGroup(myT)}
    if not client['host']: # We couldn't get an IP address for the target host
        return twisted.internet.defer.fail(Exception("Not enough information about client to perform deletion")).addErrback(parseDeleteError, myCommandOnHostID, decrement_attempts_left = True, error_code = PULSE2_TARGET_NOTENOUGHINFO_ERROR)

    # if we are here, deletion has either previously failed or never be done
    if re.compile('^file://').match(myT.mirrors): # delete from remote push
        files_list = map(lambda(a): a.split('/').pop(), myC.files.split("\n"))

        myCoH.setDeleteInProgress()
        myCoH.setStateDeleteInProgress()
        if SchedulerConfig().mode == 'sync':
            updateHistory(myCommandOnHostID, 'delete_in_progress')
            mydeffered = callOnBestLauncher(
                myCommandOnHostID,
                'sync_remote_delete',
                False,
                myCommandOnHostID,
                client,
                files_list,
                SchedulerConfig().max_command_time
            )
            mydeffered.\
                addCallback(parseDeleteResult, myCommandOnHostID).\
                addErrback(parseDeleteError, myCommandOnHostID)
        elif SchedulerConfig().mode == 'async':
            mydeffered = callOnBestLauncher(
                myCommandOnHostID,
                'async_remote_delete',
                False,
                myCommandOnHostID,
                client,
                files_list,
                SchedulerConfig().max_command_time
            )
            mydeffered.\
                addCallback(parseDeleteOrder, myCommandOnHostID).\
                addErrback(parseDeleteError, myCommandOnHostID)
        else:
            return runGiveUpPhase(myCommandOnHostID)
        return mydeffered
    else: # delete from remote pull
        mirrors = myT.mirrors.split('||')
        mirror = mirrors[0] # TODO: handle when several mirrors are available
        if re.compile('^http://').match(mirror) or re.compile('^https://').match(mirror): # HTTP download
            files_list = map(lambda(a): a.split('/').pop(), myC.files.split("\n"))

            myCoH.setDeleteInProgress()
            myCoH.setStateDeleteInProgress()
            if SchedulerConfig().mode == 'sync':
                updateHistory(myCommandOnHostID, 'delete_in_progress')
                mydeffered = callOnBestLauncher(
                    myCommandOnHostID,
                    'sync_remote_delete',
                    False,
                    myCommandOnHostID,
                    client,
                    files_list,
                    SchedulerConfig().max_command_time
                )
                mydeffered.\
                    addCallback(parseDeleteResult, myCommandOnHostID).\
                    addErrback(parseDeleteError, myCommandOnHostID)
            elif SchedulerConfig().mode == 'async':
                mydeffered = callOnBestLauncher(
                    myCommandOnHostID,
                    'async_remote_delete',
                    False,
                    myCommandOnHostID,
                    client,
                    files_list,
                    SchedulerConfig().max_command_time
                )
                mydeffered.\
                    addCallback(parseDeleteOrder, myCommandOnHostID).\
                    addErrback(parseDeleteError, myCommandOnHostID)
            else:
                return runGiveUpPhase(myCommandOnHostID)
            return mydeffered
        elif re.compile('^smb://').match(mirror): # TODO: NET download
            pass
        elif re.compile('^ftp://').match(mirror): # TODO: FTP download
            pass
        elif re.compile('^nfs://').match(mirror): # TODO: NFS download
            pass
        elif re.compile('^ssh://').match(mirror): # TODO: SSH download
            pass
        elif re.compile('^rsync://').match(mirror): # TODO: RSYNC download
            pass
        else: # do nothing
            pass

    myCoH.setDeleteIgnored()
    myCoH.setStateScheduled()
    return runInventoryPhase(myCommandOnHostID)

def runInventoryPhase(myCommandOnHostID):
    # Run inventory if needed
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logger = logging.getLogger()
    logger.info("command_on_host #%s: inventory phase" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isInventoryRunning(): # inventory still running, immediately returns
        logger.info("command_on_host #%s: still inventoriing" % myCoH.getId())
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isInventoryIgnored(): # inventory has already been ignored, jump to next stage
        logger.info("command_on_host #%s: inventory ignored" % myCoH.getId())
        return runRebootPhase(myCommandOnHostID)
    if myCoH.isInventoryDone(): # inventory has already already done, jump to next stage
        logger.info("command_on_host #%s: inventory done" % myCoH.getId())
        return runRebootPhase(myCommandOnHostID)
    if myC.isPartOfABundle() and not isLastToInventoryInBundle(myCommandOnHostID): # there is still a coh in the same bundle that has to launch inventory, jump to next stage
        logger.info("command_on_host #%s: another coh from the same bundle will launch the inventory" % myCommandOnHostID)
        myCoH.setInventoryIgnored()
        myCoH.setStateScheduled()
        return runRebootPhase(myCommandOnHostID)
    if not myCoH.isInventoryImminent(): # nothing to do right now, give out
        logger.info("command_on_host #%s: nothing to inventory right now" % myCoH.getId())
        return runGiveUpPhase(myCommandOnHostID)
    if not myC.hasToRunInventory(): # no inventory to perform, jump to next stage
        logger.info("command_on_host #%s: nothing to inventory" % myCoH.getId())
        myCoH.setInventoryIgnored()
        myCoH.setStateScheduled()
        return runRebootPhase(myCommandOnHostID)

    client = { 'host': chooseClientIP(myT), 'uuid': myT.getUUID(), 'maxbw': myC.maxbw, 'protocol': 'ssh', 'client_check': getClientCheck(myT), 'server_check': getServerCheck(myT), 'action': getAnnounceCheck('inventory'), 'group': getClientGroup(myT)}
    if not client['host']: # We couldn't get an IP address for the target host
        return twisted.internet.defer.fail(Exception("Not enough information about client to perform inventory")).addErrback(parseInventoryError, myCommandOnHostID, decrement_attempts_left = True, error_code = PULSE2_TARGET_NOTENOUGHINFO_ERROR)

    # if we are here, inventory has either previously failed or never be done
    myCoH.setInventoryInProgress()
    myCoH.setStateInventoryInProgress()
    if SchedulerConfig().mode == 'sync':
        updateHistory(myCommandOnHostID, 'inventory_in_progress')
        mydeffered = callOnBestLauncher(
            myCommandOnHostID,
            'sync_remote_inventory',
            False,
            myCommandOnHostID,
            client,
            SchedulerConfig().max_command_time
        )
        mydeffered.\
            addCallback(parseInventoryResult, myCommandOnHostID).\
            addErrback(parseInventoryError, myCommandOnHostID)
    elif SchedulerConfig().mode == 'async':
        mydeffered = callOnBestLauncher(
            myCommandOnHostID,
            'async_remote_inventory',
            False,
            myCommandOnHostID,
            client,
            SchedulerConfig().max_command_time
        )
        mydeffered.\
            addCallback(parseInventoryOrder, myCommandOnHostID).\
            addErrback(parseInventoryError, myCommandOnHostID)
    else:
        return runGiveUpPhase(myCommandOnHostID)
    return mydeffered

def runRebootPhase(myCommandOnHostID):
    # Run reboot if needed
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logger = logging.getLogger()
    logger.info("command_on_host #%s: reboot phase" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isRebootRunning(): # reboot still running, immediately returns
        logger.info("command_on_host #%s: still rebooting" % myCoH.getId())
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isRebootIgnored(): # reboot has already been ignored, jump to next stage
        logger.info("command_on_host #%s: reboot ignored" % myCoH.getId())
        return runHaltOnDone(myCommandOnHostID)
    if myCoH.isRebootDone(): # reboot has already been done, jump to next stage
        logger.info("command_on_host #%s: reboot done" % myCoH.getId())
        return runHaltOnDone(myCommandOnHostID)
    if not myCoH.isRebootImminent(): # nothing to do right now, give out
        logger.info("command_on_host #%s: do not reboot right now" % myCoH.getId())
        return runGiveUpPhase(myCommandOnHostID)
    if not myC.hasToReboot(): # no reboot to perform, jump to next stage
        logger.info("command_on_host #%s: do not reboot" % myCoH.getId())
        myCoH.setRebootIgnored()
        myCoH.setStateScheduled()
        return runHaltOnDone(myCommandOnHostID)

    client = { 'host': chooseClientIP(myT), 'uuid': myT.getUUID(), 'maxbw': myC.maxbw, 'protocol': 'ssh', 'client_check': getClientCheck(myT), 'server_check': getServerCheck(myT), 'action': getAnnounceCheck('reboot'), 'group': getClientGroup(myT)}
    if not client['host']: # We couldn't get an IP address for the target host
        return twisted.internet.defer.fail(Exception("Not enough information about client to perform reboot")).addErrback(parseRebootError, myCommandOnHostID, decrement_attempts_left = True, error_code = PULSE2_TARGET_NOTENOUGHINFO_ERROR)

    myCoH.setRebootInProgress()
    myCoH.setStateRebootInProgress()

    if SchedulerConfig().mode == 'sync':
        updateHistory(myCommandOnHostID, 'reboot_in_progress')
        mydeffered = callOnBestLauncher(
            myCommandOnHostID,
            'sync_remote_reboot',
            False,
            myCommandOnHostID,
            client,
            SchedulerConfig().max_command_time
        )
        mydeffered.\
            addCallback(parseRebootResult, myCommandOnHostID).\
            addErrback(parseRebootError, myCommandOnHostID)
    elif SchedulerConfig().mode == 'async':
        mydeffered = callOnBestLauncher(
            myCommandOnHostID,
            'async_remote_reboot',
            False,
            myCommandOnHostID,
            client,
            SchedulerConfig().max_command_time
        )
        mydeffered.\
            addCallback(parseRebootOrder, myCommandOnHostID).\
            addErrback(parseRebootError, myCommandOnHostID)
    else:
        return runGiveUpPhase(myCommandOnHostID)
    return mydeffered

def runHaltOnDone(myCommandOnHostID): # supposed to be called at the very end of the process
    logger = logging.getLogger()
    logger.info("command_on_host #%s: halt-on-done phase" % myCommandOnHostID)
    return runHaltPhase(myCommandOnHostID, 'done')

def runHaltOnFailed(myCommandOnHostID): # supposed to be called when the command is trashed
    logger = logging.getLogger()
    logger.info("command_on_host #%s: halt-on-failed phase" % myCommandOnHostID)
    return runHaltPhase(myCommandOnHostID, 'failed')

def runHaltPhase(myCommandOnHostID, condition):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logger = logging.getLogger()
    logger.info("command_on_host #%s: halt phase" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isHaltRunning(): # halt still running, immediately returns
        logging.getLogger().info("command_on_host #%s: still halting" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    if myCoH.isHaltIgnored(): # halt has already be ignored, jump to next stage
        logger.info("command_on_host #%s: halt ignored" % myCommandOnHostID)
        return runDonePhase(myCommandOnHostID)
    if myCoH.isHaltDone(): # halt has already be done, jump to next stage
        logger.info("command_on_host #%s: halt done" % myCommandOnHostID)
        return runDonePhase(myCommandOnHostID)
    if myC.isPartOfABundle() and not isLastToHaltInBundle(myCommandOnHostID): # there is still a coh in the same bundle that has to halt, jump to next stage
        logger.info("command_on_host #%s: another coh from the same bundle will do the halt" % myCommandOnHostID)
        myCoH.setHaltIgnored()
        return runDonePhase(myCommandOnHostID)
    if not myCoH.isHaltImminent(): # nothing to do right now, give out
        logger.info("command_on_host #%s: do not halt right now" % myCoH.getId())
        return runGiveUpPhase(myCommandOnHostID)
    if not myC.hasToHalt(): # do not run halt
        logger.info("command_on_host #%s: halt ignored" % myCommandOnHostID)
        myCoH.setHaltIgnored()
        myCoH.setStateScheduled()
        return runDonePhase(myCommandOnHostID)
    if condition == 'done' and not myC.hasToHaltIfDone(): # halt on done and we do not have to halt on done
        logger.info("command_on_host #%s: halt-on-done ignored" % myCommandOnHostID)
        myCoH.setHaltIgnored()
        myCoH.setStateScheduled()
        return runDonePhase(myCommandOnHostID)
    if condition == 'failed' and not myC.hasToHaltIfFailed(): # halt on failed and we do not have to halt on failure
        logger.info("command_on_host #%s: halt-on-failed ignored" % myCommandOnHostID)
        myCoH.setHaltIgnored()
        myCoH.setStateScheduled()
        return runDonePhase(myCommandOnHostID)

    client = { 'host': chooseClientIP(myT), 'uuid': myT.getUUID(), 'maxbw': myC.maxbw, 'protocol': 'ssh', 'client_check': getClientCheck(myT), 'server_check': getServerCheck(myT), 'action': getAnnounceCheck('halt'), 'group': getClientGroup(myT)}
    if not client['host']: # We couldn't get an IP address for the target host
        return twisted.internet.defer.fail(Exception("Not enough information about client to perform halt")).addErrback(parseHaltError, myCommandOnHostID, decrement_attempts_left = True, error_code = PULSE2_TARGET_NOTENOUGHINFO_ERROR)

    myCoH.setHaltInProgress()
    myCoH.setStateHaltInProgress()

    if SchedulerConfig().mode == 'sync':
        updateHistory(myCommandOnHostID, 'halt_in_progress')
        mydeffered = callOnBestLauncher(
            myCommandOnHostID,
            'sync_remote_halt',
            False,
            myCommandOnHostID,
            client,
            SchedulerConfig().max_command_time
        )
        mydeffered.\
            addCallback(parseHaltResult, myCommandOnHostID).\
            addErrback(parseHaltError, myCommandOnHostID)
    elif SchedulerConfig().mode == 'async':
        mydeffered = callOnBestLauncher(
            myCommandOnHostID,
            'async_remote_halt',
            False,
            myCommandOnHostID,
            client,
            SchedulerConfig().max_command_time
        )
        mydeffered.\
            addCallback(parseHaltOrder, myCommandOnHostID).\
            addErrback(parseHaltError, myCommandOnHostID)
    else:
        return runGiveUpPhase(myCommandOnHostID)
    return mydeffered

def parseWOLAttempt(attempt_result, myCommandOnHostID):

    def setstate(myCommandOnHostID, stdout, stderr):
        (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
        logging.getLogger().info("command_on_host #%s: WOL done and done waiting" % (myCommandOnHostID))
        if myCoH == None:
            return runGiveUpPhase(myCommandOnHostID)
        updateHistory(myCommandOnHostID, 'wol_done', PULSE2_SUCCESS_ERROR, stdout, stderr)
        if myCoH.switchToWOLDone():
            return runUploadPhase(myCommandOnHostID)
        else:
            return runGiveUpPhase(myCommandOnHostID)

    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    try:
        (exitcode, stdout, stderr) = attempt_result
    except TypeError,e: # xmlrpc call failed
        logging.getLogger().error("command_on_host #%s: WOL request seems to have failed ?!" % (myCommandOnHostID))

        myCoH.setStateScheduled()
        myCoH.setWOLToDo()
        myCoH.resetLastWOLAttempt()
        return runGiveUpPhase(myCommandOnHostID)

    myCoH.setLastWOLAttempt()
    logging.getLogger().info("command_on_host #%s: WOL done, now waiting %s seconds for the computer to wake up" % (myCommandOnHostID,SchedulerConfig().max_wol_time))
    twisted.internet.reactor.callLater(SchedulerConfig().max_wol_time, setstate, myCommandOnHostID, stdout, stderr)
    return runGiveUpPhase(myCommandOnHostID)

def parsePushResult((exitcode, stdout, stderr), myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if exitcode == PULSE2_SUCCESS_ERROR: # success
        logging.getLogger().info("command_on_host #%s: push done (exitcode == 0)" % myCommandOnHostID)
        updateHistory(myCommandOnHostID, 'upload_done', exitcode, stdout, stderr)
        if myCoH.switchToUploadDone():
            return runExecutionPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failure: immediately give up
        logging.getLogger().info("command_on_host #%s: push failed (exitcode != 0)" % myCommandOnHostID)
        updateHistory(myCommandOnHostID, 'upload_failed', exitcode, stdout, stderr)
        if not myCoH.switchToUploadFailed(myC.getNextConnectionDelay()):
            return runFailedPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parsePullResult((exitcode, stdout, stderr), myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)

    proxy_coh_id = myCoH.getUsedProxy()
    if proxy_coh_id:
        (myProxyCoH, myProxyC, myProxyT) = gatherCoHStuff(proxy_coh_id)
        proxy_uuid = myProxyT.getUUID()
        # see if we can unload a proxy
        # no ret val
        LocalProxiesUsageTracking().untake(proxy_uuid, myC.getId())
        logging.getLogger().debug("scheduler %s: coh #%s used coh #%s as local proxy, releasing one slot (%d left)" % (SchedulerConfig().name, myCommandOnHostID, proxy_coh_id, LocalProxiesUsageTracking().how_much_left_for(proxy_uuid, myC.getId())))

    if exitcode == PULSE2_SUCCESS_ERROR: # success
        logging.getLogger().info("command_on_host #%s: pull done (exitcode == 0)" % myCommandOnHostID)
        updateHistory(myCommandOnHostID, 'upload_done', exitcode, stdout, stderr)
        if myCoH.switchToUploadDone():
            return runExecutionPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failure: immediately give up
        logging.getLogger().info("command_on_host #%s: pull failed (exitcode != 0)" % myCommandOnHostID)
        updateHistory(myCommandOnHostID, 'upload_failed', exitcode, stdout, stderr)
        if not myCoH.switchToUploadFailed(myC.getNextConnectionDelay()):
            return runFailedPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseExecutionResult((exitcode, stdout, stderr), myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if exitcode == PULSE2_SUCCESS_ERROR: # success
        logging.getLogger().info("command_on_host #%s: execution done (exitcode == 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'execution_done', exitcode, stdout, stderr)
        if myCoH.switchToExecutionDone():
            return runDeletePhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failure: immediately give up
        logging.getLogger().info("command_on_host #%s: execution failed (exitcode != 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'execution_failed', exitcode, stdout, stderr)
        if not myCoH.switchToExecutionFailed(myC.getNextConnectionDelay()):
            return runFailedPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseDeleteResult((exitcode, stdout, stderr), myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if exitcode == PULSE2_SUCCESS_ERROR: # success
        logging.getLogger().info("command_on_host #%s: delete done (exitcode == 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'delete_done', exitcode, stdout, stderr)
        if myCoH.switchToDeleteDone():
            return runInventoryPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failure: immediately give up
        logging.getLogger().info("command_on_host #%s: delete failed (exitcode != 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'delete_failed', exitcode, stdout, stderr)
        if not myCoH.switchToDeleteFailed(myC.getNextConnectionDelay()):
            return runFailedPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseInventoryResult((exitcode, stdout, stderr), myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if exitcode == PULSE2_SUCCESS_ERROR: # success
        logging.getLogger().info("command_on_host #%s: inventory done (exitcode == 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'inventory_done', exitcode, stdout, stderr)
        if myCoH.switchToInventoryDone():
            return runRebootPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failure: immediately give up
        logging.getLogger().info("command_on_host #%s: inventory failed (exitcode != 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'inventory_failed', exitcode, stdout, stderr)
        if not myCoH.switchToInventoryFailed(myC.getNextConnectionDelay()):
            return runFailedPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseRebootResult((exitcode, stdout, stderr), myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    logger = logging.getLogger()
    if exitcode == PULSE2_SUCCESS_ERROR: # success
        logging.getLogger().info("command_on_host #%s: reboot done (exitcode == 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'reboot_done', exitcode, stdout, stderr)
        if myCoH.switchToRebootDone():
            return runHaltOnDone(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failure: immediately give up
        logging.getLogger().info("command_on_host #%s: reboot failed (exitcode != 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'reboot_failed', exitcode, stdout, stderr)
        if not myCoH.switchToRebootFailed(myC.getNextConnectionDelay()):
            return runFailedPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseHaltResult((exitcode, stdout, stderr), myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    logger = logging.getLogger()
    if exitcode == PULSE2_SUCCESS_ERROR: # success
        logging.getLogger().info("command_on_host #%s: halt done (exitcode == 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'halt_done', exitcode, stdout, stderr)
        if myCoH.switchToHaltDone():
            return runDonePhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failure: immediately give up
        logging.getLogger().info("command_on_host #%s: halt failed (exitcode != 0)" % (myCommandOnHostID))
        updateHistory(myCommandOnHostID, 'halt_failed', exitcode, stdout, stderr)
        if myCoH.switchToHaltFailed(myC.getNextConnectionDelay()):
            return runFailedPhase(myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parsePushOrder(taken_in_account, myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if taken_in_account: # success
        updateHistory(myCommandOnHostID, 'upload_in_progress')
        logging.getLogger().info("command_on_host #%s: push order taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failed: launcher seems to have rejected it
        myCoH.setUploadToDo()
        myCoH.setStateScheduled()
        logging.getLogger().warn("command_on_host #%s: push order not taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parsePullOrder(taken_in_account, myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if taken_in_account: # success
        updateHistory(myCommandOnHostID, 'upload_in_progress')
        logging.getLogger().info("command_on_host #%s: pull order taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failed: launcher seems to have rejected it
        myCoH.setUploadToDo()
        myCoH.setStateScheduled()
        logging.getLogger().warn("command_on_host #%s: pull order not taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseExecutionOrder(taken_in_account, myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if taken_in_account: # success
        updateHistory(myCommandOnHostID, 'execution_in_progress')
        logging.getLogger().info("command_on_host #%s: execution order taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failed: launcher seems to have rejected it
        myCoH.setExecutionToDo()
        myCoH.setStateScheduled()
        logging.getLogger().warn("command_on_host #%s: execution order not taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseDeleteOrder(taken_in_account, myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if taken_in_account: # success
        updateHistory(myCommandOnHostID, 'delete_in_progress')
        logging.getLogger().info("command_on_host #%s: delete order taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failed: launcher seems to have rejected it
        myCoH.setDeleteToDo()
        myCoH.setStateScheduled()
        logging.getLogger().warn("command_on_host #%s: delete order not taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseInventoryOrder(taken_in_account, myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if taken_in_account: # success
        updateHistory(myCommandOnHostID, 'inventory_in_progress')
        logging.getLogger().info("command_on_host #%s: inventory order taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failed: launcher seems to have rejected it
        myCoH.setInventoryToDo()
        myCoH.setStateScheduled()
        logging.getLogger().warn("command_on_host #%s: inventory order not taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseRebootOrder(taken_in_account, myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if taken_in_account: # success
        updateHistory(myCommandOnHostID, 'reboot_in_progress')
        logging.getLogger().info("command_on_host #%s: reboot order taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failed: launcher seems to have rejected it
        myCoH.setRebootToDo()
        myCoH.setStateScheduled()
        logging.getLogger().warn("command_on_host #%s: reboot order not taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseHaltOrder(taken_in_account, myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    if taken_in_account: # success
        updateHistory(myCommandOnHostID, 'halt_in_progress')
        logging.getLogger().info("command_on_host #%s: halt order taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)
    else: # failed: launcher seems to have rejected it
        myCoH.setHaltToDo()
        myCoH.setStateScheduled()
        logging.getLogger().warn("command_on_host #%s: halt order not taken in account" % myCommandOnHostID)
        return runGiveUpPhase(myCommandOnHostID)

def parseWOLError(reason, myCommandOnHostID, decrement_attempts_left = False, error_code = PULSE2_UNKNOWN_ERROR):
    """
       decrement_attempts_left : by default do not decrement tries as the error has most likeley be produced by an internal condition
       error_code : by default we consider un unknwo error was raised (PULSE2_UNKNOWN_ERROR)
    """
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().warn("command_on_host #%s: WOL failed" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    updateHistory(myCommandOnHostID, 'wol_failed', error_code, '', reason.getErrorMessage())
    if not myCoH.switchToWOLFailed(myC.getNextConnectionDelay(), decrement_attempts_left):
        return runFailedPhase(myCommandOnHostID)
    return runGiveUpPhase(myCommandOnHostID)

def parsePushError(reason, myCommandOnHostID, decrement_attempts_left = False, error_code = PULSE2_UNKNOWN_ERROR):
    """
       decrement_attempts_left : by default do not decrement tries as the error has most likeley be produced by an internal condition
       error_code : by default we consider un unknwo error was raised (PULSE2_UNKNOWN_ERROR)
    """
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().warn("command_on_host #%s: push failed, unattented reason: %s" % (myCommandOnHostID, reason))
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    updateHistory(myCommandOnHostID, 'upload_failed', error_code, '', reason.getErrorMessage())
    if not myCoH.switchToUploadFailed(myC.getNextConnectionDelay(), decrement_attempts_left):
        return runFailedPhase(myCommandOnHostID)
    return runGiveUpPhase(myCommandOnHostID)

def parsePullError(reason, myCommandOnHostID, decrement_attempts_left = False, error_code = PULSE2_UNKNOWN_ERROR):
    """
       decrement_attempts_left : by default do not decrement tries as the error has most likeley be produced by an internal condition
       error_code : by default we consider un unknwo error was raised (PULSE2_UNKNOWN_ERROR)
    """
    # something goes really wrong: immediately give up
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().warn("command_on_host #%s: pull failed, unattented reason: %s" % (myCommandOnHostID, reason))
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)

    proxy_coh_id = myCoH.getUsedProxy()
    if proxy_coh_id:
        (myProxyCoH, myProxyC, myProxyT) = gatherCoHStuff(proxy_coh_id)
        proxy_uuid = myProxyT.getUUID()
        # see if we can unload a proxy
        # no ret val
        LocalProxiesUsageTracking().untake(proxy_uuid, myC.getId())
        logging.getLogger().debug("scheduler %s: coh #%s used coh #%s as local proxy, releasing one slot (%d left)" % (SchedulerConfig().name, myCommandOnHostID, proxy_coh_id, LocalProxiesUsageTracking().how_much_left_for(proxy_uuid, myC.getId())))

    updateHistory(myCommandOnHostID, 'upload_failed', error_code, '', reason.getErrorMessage())
    if not myCoH.switchToUploadFailed(myC.getNextConnectionDelay(), decrement_attempts_left):
        return runFailedPhase(myCommandOnHostID)
    return runGiveUpPhase(myCommandOnHostID)

def parseExecutionError(reason, myCommandOnHostID, decrement_attempts_left = False, error_code = PULSE2_UNKNOWN_ERROR):
    """
       decrement_attempts_left : by default do not decrement tries as the error has most likeley be produced by an internal condition
       error_code : by default we consider un unknwo error was raised (PULSE2_UNKNOWN_ERROR)
    """
    # something goes really wrong: immediately give up
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().warn("command_on_host #%s: execution failed, unattented reason: %s" % (myCommandOnHostID, reason))
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    updateHistory(myCommandOnHostID, 'execution_failed', error_code, '', reason.getErrorMessage())
    if not myCoH.switchToExecutionFailed(myC.getNextConnectionDelay(), decrement_attempts_left):
        return runFailedPhase(myCommandOnHostID)
    # FIXME: should return a failure (but which one ?)
    return runGiveUpPhase(myCommandOnHostID)

def parseDeleteError(reason, myCommandOnHostID, decrement_attempts_left = False, error_code = PULSE2_UNKNOWN_ERROR):
    """
       decrement_attempts_left : by default do not decrement tries as the error has most likeley be produced by an internal condition
       error_code : by default we consider un unknwo error was raised (PULSE2_UNKNOWN_ERROR)
    """
    # something goes really wrong: immediately give up
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().warn("command_on_host #%s: delete failed, unattented reason: %s" % (myCommandOnHostID, reason))
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    updateHistory(myCommandOnHostID, 'delete_failed', error_code, '', reason.getErrorMessage())
    if not myCoH.switchToDeleteFailed(myC.getNextConnectionDelay(), decrement_attempts_left):
        return runFailedPhase(myCommandOnHostID)
    # FIXME: should return a failure (but which one ?)
    return runGiveUpPhase(myCommandOnHostID)

def parseInventoryError(reason, myCommandOnHostID, decrement_attempts_left = False, error_code = PULSE2_UNKNOWN_ERROR):
    """
       decrement_attempts_left : by default do not decrement tries as the error has most likeley be produced by an internal condition
       error_code : by default we consider un unknwo error was raised (PULSE2_UNKNOWN_ERROR)
    """
    # something goes really wrong: immediately give up
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logger = logging.getLogger()
    logger.warn("command_on_host #%s: inventory failed, unattented reason: %s" % (myCommandOnHostID, reason))
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    updateHistory(myCommandOnHostID, 'inventory_failed', error_code, '', reason.getErrorMessage())
    if not myCoH.switchToInventoryFailed(myC.getNextConnectionDelay(), decrement_attempts_left):
        return runFailedPhase(myCommandOnHostID)
    # FIXME: should return a failure (but which one ?)
    return runGiveUpPhase(myCommandOnHostID)

def parseRebootError(reason, myCommandOnHostID, decrement_attempts_left = False, error_code = PULSE2_UNKNOWN_ERROR):
    """
       decrement_attempts_left : by default do not decrement tries as the error has most likeley be produced by an internal condition
       error_code : by default we consider un unknwo error was raised (PULSE2_UNKNOWN_ERROR)
    """
    # something goes really wrong: immediately give up
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logger = logging.getLogger()
    logger.warn("command_on_host #%s: reboot failed, unattented reason: %s" % (myCommandOnHostID, reason))
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    updateHistory(myCommandOnHostID, 'reboot_failed', error_code, '', reason.getErrorMessage())
    if not myCoH.switchToRebootFailed(myC.getNextConnectionDelay(), decrement_attempts_left):
        return runFailedPhase(myCommandOnHostID)
    # FIXME: should return a failure (but which one ?)
    return runGiveUpPhase(myCommandOnHostID)

def parseHaltError(reason, myCommandOnHostID, decrement_attempts_left = False, error_code = PULSE2_UNKNOWN_ERROR):
    """
       decrement_attempts_left : by default do not decrement tries as the error has most likeley be produced by an internal condition
       error_code : by default we consider un unknwo error was raised (PULSE2_UNKNOWN_ERROR)
    """
    # something goes really wrong: immediately give up
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logger = logging.getLogger()
    logger.warn("command_on_host #%s: halt failed, unattented reason: %s" % (myCommandOnHostID, reason))
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    updateHistory(myCommandOnHostID, 'halt_failed', error_code, '', reason.getErrorMessage())
    if not myCoH.switchToHaltFailed(myC.getNextConnectionDelay(), decrement_attempts_left):
        return runFailedPhase(myCommandOnHostID)
    # FIXME: should return a failure (but which one ?)
    return runGiveUpPhase(myCommandOnHostID)

def runDonePhase(myCommandOnHostID):
    # Last step : end file
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().info("command_on_host #%s: end (done) phase" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    myCoH.setStateDone()
    return runGiveUpPhase(myCommandOnHostID)

def runFailedPhase(myCommandOnHostID):
    # Last step : end file
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().info("command_on_host #%s: end (failed) phase" % myCommandOnHostID)
    if myCoH == None:
        return runGiveUpPhase(myCommandOnHostID)
    myCoH.setStateFailed()
    return runGiveUpPhase(myCommandOnHostID)

def runGiveUpPhase(myCommandOnHostID):
    (myCoH, myC, myT) = gatherCoHStuff(myCommandOnHostID)
    logging.getLogger().info("command_on_host #%s: Giving up" % myCommandOnHostID)
    if SchedulerConfig().lock_processed_commands:
        CommandsOnHostTracking().release(myCommandOnHostID)
    return None

def updateHistory(id, state = None, error_code = PULSE2_SUCCESS_ERROR, stdout = '', stderr = ''):
    encoding = SchedulerConfig().dbencoding
    history = CommandsHistory()
    history.fk_commands_on_host = id
    history.date = time.time()
    history.error_code = error_code
    history.stdout = stdout.encode(encoding, 'replace')
    history.stderr = stderr.encode(encoding, 'replace')
    history.state = state
    history.flush()

def chooseClientIP(myT):
    return pulse2.scheduler.network.chooseClientIP({
        'uuid': myT.getUUID(),
        'fqdn': myT.getFQDN(),
        'shortname': myT.getShortName(),
        'ips': myT.getIps(),
        'macs': myT.getMacs()
    })

def getClientGroup(myT):
    return MGAssignAlgoManager().getMachineGroup(myT)

def getMaxNumberOfGroups():
    return MGAssignAlgoManager().getMaxNumberOfGroups()

def getClientCheck(myT):
    return getCheck(SchedulerConfig().client_check, {
        'uuid': myT.getUUID(),
        'shortname': myT.getShortName(),
        'ips': myT.getIps(),
        'macs': myT.getMacs()
    });

def getServerCheck(myT):
    return getCheck(SchedulerConfig().server_check, {
        'uuid': myT.getUUID(),
        'shortname': myT.getShortName(),
        'ips': myT.getIps(),
        'macs': myT.getMacs()
    });
