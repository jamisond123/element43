import datetime
import pytz

import eveapi

from django.db import IntegrityError

from celery.task import Task, PeriodicTask
from celery.task.schedules import crontab
from celery.utils.log import get_task_logger

from apps.common.util import cast_empty_string_to_int, cast_empty_string_to_float
from apps.api.models import *
from api_exceptions import handle_api_exception

from eve_db.models import StaStation, MapSolarSystem
from apps.market_data.models import Orders

logger = get_task_logger(__name__)

class ProcessConquerableStations(PeriodicTask):
    """
    Updates conquerable stations.
    """

    run_every = datetime.timedelta(hours=1)

    def run(self, **kwargs):
        logger.debug('Updating conquerable stations...')

        api = eveapi.EVEAPIConnection()
        stations = api.eve.ConquerableStationList()

        for station in stations.outposts:
            # Try to find mapping in DB. If found -> update. If not found -> create
            try:
                station_object = StaStation.objects.get(id=station.stationID)
                station_object.name = station.stationName
                station_object.save()

            except StaStation.DoesNotExist:

                # Add station / catch race condition with other workers
                try:
                    station_object = StaStation(id=station.stationID,
                                                name=station.stationName,
                                                solar_system_id=station.solarSystemID,
                                                type_id=station.stationTypeID,
                                                constellation=MapSolarSystem.objects.get(id=station.solarSystemID).constellation,
                                                region=MapSolarSystem.objects.get(id=station.solarSystemID).region)
                    station_object.save()
                except IntegrityError:
                    logger.warning('Station was already processed by another concurrently running worker.')

        logger.info('Updated %d conquerable stations.' % len(stations.outposts))


class ProcessResearch(PeriodicTask):
    """
    Updates the research agents for all characters.
    """

    run_every = datetime.timedelta(minutes=5)

    def run(self, **kwargs):
        update_timers = APITimer.objects.filter(apisheet="Research",
                                                nextupdate__lte=pytz.utc.localize(datetime.datetime.utcnow()))

        for update in update_timers:
            ProcessResearchCharacter.apply_async(args=[update.character_id], expires=datetime.datetime.now() + datetime.timedelta(hours=1))

        logger.info('Scheduled %d research agent updates.' % len(update_timers))


class ProcessResearchCharacter(Task):
    """
    Run the actual update.
    """

    def run(self, character_id):

        api = eveapi.EVEAPIConnection()
        character = Character.objects.get(id=character_id)

        # Try to fetch a valid key from DB
        try:
            apikey = APIKey.objects.get(id=character.apikey_id, is_valid=True)
        except APIKey.DoesNotExist:
            # End execution for this character
            return

        logger.debug("Updating research agents for %s..." % character.name)

        # Try to authenticate and handle exceptions properly
        try:
            auth = api.auth(keyID=apikey.keyid, vCode=apikey.vcode)
            me = auth.character(character.id)

            # Get newest page - use maximum row count to minimize amount of requests
            sheet = me.Research()

        except eveapi.Error, e:
            handle_api_exception(e, apikey)
            return

        # Clear all existing jobs for this character and add new ones. We don't want to keep expired data.
        Research.objects.filter(character=character).delete()

        for job in sheet.research:
            new_job = Research(character=character,
                               agent_id=job.agentID,
                               skill_id=job.skillTypeID,
                               start_date=pytz.utc.localize(datetime.datetime.utcfromtimestamp(job.researchStartDate)),
                               points_per_day=job.pointsPerDay,
                               remainder_points=job.remainderPoints)
            new_job.save()

        # Update timer
        timer = APITimer.objects.get(character=character, apisheet='Research')
        timer.nextupdate = pytz.utc.localize(datetime.datetime.utcfromtimestamp(sheet._meta.cachedUntil))
        timer.save()

        logger.debug(" %s's research import was completed successfully." % character.name)


class ProcessWalletTransactions(PeriodicTask):
    """
    Processes char/corp wallet transactions.
    TODO: Add corp key handling.
    """

    run_every = datetime.timedelta(minutes=5)

    def run(self, **kwargs):

        update_timers = APITimer.objects.filter(apisheet="WalletTransactions",
                                                nextupdate__lte=pytz.utc.localize(datetime.datetime.utcnow()))

        for update in update_timers:

            ProcessWalletTransactionsCharacter.apply_async(args=[update.character_id], expires=datetime.datetime.now() + datetime.timedelta(hours=1))

        logger.info('Scheduled %d transaction updates.' % len(update_timers))


class ProcessWalletTransactionsCharacter(Task):
    """
    Run the actual update.
    """

    def run(self, character_id):

        api = eveapi.EVEAPIConnection()
        character = Character.objects.get(id=character_id)

        # Try to fetch a valid key from DB
        try:
            apikey = APIKey.objects.get(id=character.apikey_id, is_valid=True)
        except APIKey.DoesNotExist:
            # End execution for this character
            return

        logger.debug("Updating transactions for %s..." % character.name)

        # Try to authenticate and handle exceptions properly
        try:
            auth = api.auth(keyID=apikey.keyid, vCode=apikey.vcode)
            me = auth.character(character.id)

            # Get newest page - use maximum row count to minimize amount of requests
            sheet = me.WalletTransactions(rowCount=2560)

        except eveapi.Error, e:
            handle_api_exception(e, apikey)
            return

        walking = True

        while walking:

            # Check if new set contains any entries
            if len(sheet.transactions):

                # Get existing entries in DB in one run
                existing_entries = MarketTransaction.objects.filter(character=character).values_list('journal_transaction_id', flat=True)

                try:

                    # Process transactions
                    for transaction in sheet.transactions:

                        if transaction.journalTransactionID in existing_entries:
                            # If there already is an entry with this id, we can stop walking.
                            # So we don't walk all the way back every single time we run this task.
                            walking = False

                        else:

                            try:
                                # If it does not exist, create transaction
                                entry = MarketTransaction(character=character,
                                                          date=pytz.utc.localize(datetime.datetime.utcfromtimestamp(transaction.transactionDateTime)),
                                                          transaction_id=transaction.transactionID,
                                                          invtype_id=transaction.typeID,
                                                          quantity=transaction.quantity,
                                                          price=transaction.price,
                                                          client_id=transaction.clientID,
                                                          client_name=transaction.clientName,
                                                          station_id=transaction.stationID,
                                                          is_bid=(transaction.transactionType == 'buy'),
                                                          journal_transaction_id=transaction.journalTransactionID,
                                                          is_corporate_transaction=(transaction.transactionFor == 'corporation'))
                                entry.save()

                            # Catch integrity errors for example when the SDE is outdated and we're getting unknown typeIDs
                            except IntegrityError:
                                logger.warning('IntegrityError: Probably the SDE is outdated. typeID: %d, transactionID: %d' % (transaction.typeID, transaction.journalTransactionID))
                                continue

                # If we somehow got the same transaction multiple times in our DB, remove the redundant ones
                except MarketTransaction.MultipleObjectsReturned:
                    # Remove all duplicate items except for one
                    duplicates = MarketTransaction.objects.filter(journal_transaction_id=transaction.journalTransactionID, character=character)

                    for duplicate in duplicates[1:]:
                        logger.warning('Removing duplicate MarketTransaction with ID: %d (journalTransactionID: %d)' % (duplicate.id, duplicate.transaction_id))
                        duplicate.delete()

                # Fetch next page if we're still walking
                if walking:

                    try:
                        # Get next page based on oldest id in db - use maximum row count to minimize amount of requests
                        oldest_id = MarketTransaction.objects.filter(character=character).order_by('date')[:1][0].journal_transaction_id
                        sheet = me.WalletTransactions(rowCount=2560, fromID=oldest_id)

                    except IndexError:
                        logger.error('IndexError: %s (%d) has no valid types in his/her transaction history.' % (character.name, character.id))
                        walking = False
                        pass

            else:
                walking = False

        # Update timer
        timer = APITimer.objects.get(character=character, apisheet='WalletTransactions')
        timer.nextupdate = pytz.utc.localize(datetime.datetime.utcfromtimestamp(sheet._meta.cachedUntil))
        timer.save()

        logger.debug("%s's transaction import was completed successfully." % character.name)


class ProcessWalletJournal(PeriodicTask):
    """
    Processes char/corp journal. Done every 5 minutes.
    TODO: Add corp key handling.
    """

    run_every = datetime.timedelta(minutes=5)

    def run(self, **kwargs):
        update_timers = APITimer.objects.filter(apisheet="WalletJournal",
                                                nextupdate__lte=pytz.utc.localize(datetime.datetime.utcnow()))

        for update in update_timers:

            ProcessWalletJournalCharacter.apply_async(args=[update.character_id], expires=datetime.datetime.now() + datetime.timedelta(hours=1))

        logger.info('Scheduled %d journal updates.' % len(update_timers))


class ProcessWalletJournalCharacter(Task):
    """
    Run the actual update.
    """

    def run(self, character_id):

        api = eveapi.EVEAPIConnection()
        character = Character.objects.get(id=character_id)

        # Try to fetch a valid key from DB
        try:
            apikey = APIKey.objects.get(id=character.apikey_id, is_valid=True)
        except APIKey.DoesNotExist:
            # End execution for this character
            return

        logger.debug("Updating journal entries for %s..." % character.name)

        # Try to authenticate and handle exceptions properly
        try:
            auth = api.auth(keyID=apikey.keyid, vCode=apikey.vcode)
            me = auth.character(character.id)

            # Get newest page - use maximum row count to minimize amount of requests
            sheet = me.WalletJournal(rowCount=2560)

        except eveapi.Error, e:
            handle_api_exception(e, apikey)
            return

        walking = True

        while walking:

            # Check if new set contains any entries
            if len(sheet.transactions):

                # Get existing entries in DB in one run
                existing_entries = JournalEntry.objects.filter(character=character).values_list('ref_id', flat=True)

                # Process journal entries
                for transaction in sheet.transactions:

                    try:

                        if transaction.refID in existing_entries:
                            # If there already is an entry with this id, we can stop walking.
                            # So we don't walk all the way back every single time we run this task.
                            walking = False

                        else:
                            # Add entry to DB
                            entry = JournalEntry(ref_id=transaction.refID,
                                                 character=character,
                                                 date=pytz.utc.localize(datetime.datetime.utcfromtimestamp(transaction.date)),
                                                 ref_type_id=transaction.refTypeID,
                                                 amount=transaction.amount,
                                                 balance=transaction.balance,
                                                 owner_name_1=transaction.ownerName1,
                                                 owner_id_1=transaction.ownerID1,
                                                 owner_name_2=transaction.ownerName2,
                                                 owner_id_2=transaction.ownerID2,
                                                 arg_name_1=transaction.argName1,
                                                 arg_id_1=transaction.argID1,
                                                 reason=transaction.reason,
                                                 tax_receiver_id=cast_empty_string_to_int(transaction.taxReceiverID),
                                                 tax_amount=cast_empty_string_to_float(transaction.taxAmount))
                            entry.save()

                    # If we somehow got the same transaction multiple times in our DB, remove the redundant ones
                    except JournalEntry.MultipleObjectsReturned:
                        # Remove all duplicate items except for one
                        duplicates = JournalEntry.objects.filter(ref_id=transaction.refID, character=character)

                        for duplicate in duplicates[1:]:
                            logger.warning('Removing duplicate JournalEntry with ID: %d (refID: %d)' % (duplicate.id, duplicate.ref_id))
                            duplicate.delete()

                # Fetch next page if we're still walking
                if walking:
                    # Get next page based on oldest id in db - use maximum row count to minimize number of requests
                    oldest_id = JournalEntry.objects.filter(character=character).order_by('date')[:1][0].ref_id
                    sheet = me.WalletJournal(rowCount=2560, fromID=oldest_id)

            else:
                walking = False

        # Update timer
        timer = APITimer.objects.get(character=character, apisheet='WalletJournal')
        timer.nextupdate = pytz.utc.localize(datetime.datetime.utcfromtimestamp(sheet._meta.cachedUntil))
        timer.save()

        logger.debug("%s's journal import was completed successfully." % character.name)


class ProcessRefTypes(PeriodicTask):
    """
    Reloads the refTypeID to name mappings. Done daily at 00:00 just before history is processed.
    """

    run_every = crontab(hour=0, minute=0)

    def run(self, **kwargs):

        logger.debug('Updating refTypeIDs...')

        api = eveapi.EVEAPIConnection()
        ref_types = api.eve.RefTypes()

        for ref_type in ref_types.refTypes:
            # Try to find mapping in DB. If found -> update. If not found -> create
            try:
                type_object = RefType.objects.get(id=ref_type.refTypeID)
                type_object.name = ref_type.refTypeName
                type_object.save()

            except RefType.DoesNotExist:
                type_object = RefType(id=ref_type.refTypeID, name=ref_type.refTypeName)
                type_object.save()

        logger.info('Imported %d refTypeIDs from API.' % len(ref_types.refTypes))


class ProcessMarketOrders(PeriodicTask):
    """
    Scan the db and refresh all market orders from the API.
    Done every 5 minutes.
    """

    run_every = datetime.timedelta(minutes=5)

    def run(self, **kwargs):

        update_timers = APITimer.objects.filter(apisheet="MarketOrders",
                                                nextupdate__lte=pytz.utc.localize(datetime.datetime.utcnow()))

        for update in update_timers:

            ProcessMarketOrdersCharacter.apply_async(args=[update.character_id], expires=datetime.datetime.now() + datetime.timedelta(hours=1))

        logger.info('Scheduled %d order updates.' % len(update_timers))


class ProcessMarketOrdersCharacter(Task):
    """
    Run the actual update.
    """

    def run(self, character_id):

        api = eveapi.EVEAPIConnection()
        character = Character.objects.get(id=character_id)

        # Try to fetch a valid key from DB
        try:
            apikey = APIKey.objects.get(id=character.apikey_id, is_valid=True)
        except APIKey.DoesNotExist:
            # End execution for this character
            return

        logger.debug("Updating %s's market orders..." % character.name)

        # Try to authenticate and handle exceptions properly
        try:
            auth = api.auth(keyID=apikey.keyid, vCode=apikey.vcode)
            me = auth.character(character.id)
            orders = me.MarketOrders()

        except eveapi.Error, e:
            handle_api_exception(e, apikey)
            return

        for order in orders.orders:
            #
            # Import orders
            #

            # Look if we have this order in our DB
            try:
                db_order = Orders.objects.get(id=order.orderID)

                # Now that we found that order - let's update it
                db_order.generated_at = pytz.utc.localize(datetime.datetime.utcnow())
                db_order.price = order.price
                db_order.volume_remaining = order.volRemaining
                db_order.volume_entered = order.volEntered
                db_order.is_suspicious = False

                if order.orderState == 0:
                    db_order.is_active = True
                else:
                    db_order.is_active = False

                db_order.save()

            except Orders.DoesNotExist:

                # Try to get the station of that order to get the region/system since it isn't provided by the API
                station = StaStation.objects.get(id=order.stationID)
                region = station.region
                system = station.solar_system

                try:
                    new_order = Orders(id=order.orderID,
                                       generated_at=pytz.utc.localize(datetime.datetime.utcnow()),
                                       mapregion=region,
                                       invtype_id=order.typeID,
                                       price=order.price,
                                       volume_remaining=order.volRemaining,
                                       volume_entered=order.volEntered,
                                       minimum_volume=order.minVolume,
                                       order_range=order.range,
                                       is_bid=order.bid,
                                       issue_date=pytz.utc.localize(datetime.datetime.utcfromtimestamp(order.issued)),
                                       duration=order.duration,
                                       stastation=station,
                                       mapsolarsystem=system,
                                       is_suspicious=False,
                                       message_key='eveapi',
                                       uploader_ip_hash='eveapi',
                                       is_active=True)
                    new_order.save()
                # Catch integrity errors for example when the SDE is outdated and we're getting unknown typeIDs
                except IntegrityError:
                    logger.error('IntegrityError: Probably the SDE is outdated. typeID: %d, orderID: %d' % (order.typeID, order.orderID))
                    continue

            # Now try to get the MarketOrder
            try:
                market_order = MarketOrder.objects.get(id=order.orderID)

                # If this succeeds, update market order
                market_order.order_state = order.orderState
                market_order.save()

            except MarketOrder.DoesNotExist:
                market_order = MarketOrder(id_id=order.orderID,
                                           character=character,
                                           order_state=order.orderState,
                                           account_key=order.accountKey,
                                           escrow=order.escrow)
                market_order.save()

        # Update timer
        timer = APITimer.objects.get(character=character, apisheet='MarketOrders')
        timer.nextupdate = pytz.utc.localize(datetime.datetime.utcfromtimestamp(orders._meta.cachedUntil))
        timer.save()

        logger.debug("%s's market order import was completed successfully." % character.name)


class ProcessCharacterSheet(PeriodicTask):
    """
    Scan the db an refresh all character sheets
    Currently done once every 5 minutes
    """

    run_every = datetime.timedelta(minutes=5)

    def run(self, **kwargs):

        #scan to see if anyone is due for an update
        update_timers = APITimer.objects.filter(apisheet="CharacterSheet",
                                                nextupdate__lte=pytz.utc.localize(datetime.datetime.utcnow()))
        for update in update_timers:

            ProcessCharacterSheetCharacter.apply_async(args=[update.character_id], expires=datetime.datetime.now() + datetime.timedelta(hours=1))

        logger.info('Scheduled %d character sheet updates.' % len(update_timers))


class ProcessCharacterSheetCharacter(Task):
    """
    Run the actual update.
    """

    def run(self, character_id):

        #define variables
        i_stats = {}
        implant = {}
        attributes = ['memory', 'intelligence', 'perception', 'willpower', 'charisma']

        #grab an api object
        api = eveapi.EVEAPIConnection()
        character = Character.objects.get(id=character_id)

        # Try to fetch a valid key from DB
        try:
            apikey = APIKey.objects.get(id=character.apikey_id, is_valid=True)
        except APIKey.DoesNotExist:
            # End execution for this character
            return

        logger.debug("Updating character sheet for %s" % character.name)

        # Try to authenticate and handle exceptions properly
        try:
            auth = api.auth(keyID=apikey.keyid, vCode=apikey.vcode)
            me = auth.character(character.id)
            sheet = me.CharacterSheet()
            i_stats['name'] = ""
            i_stats['value'] = 0

        except eveapi.Error, e:
            handle_api_exception(e, apikey)
            return

        for attr in attributes:
            implant[attr] = i_stats

        # have to check because if you don't have an implant in you get nothing back
        try:
            implant['memory'] = {'name': sheet.attributeEnhancers.memoryBonus.augmentatorName,
                                 'value': sheet.attributeEnhancers.memoryBonus.augmentatorValue}
        except:
            pass
        try:
            implant['perception'] = {'name': sheet.attributeEnhancers.perceptionBonus.augmentatorName,
                                     'value': sheet.attributeEnhancers.perceptionBonus.augmentatorValue}
        except:
            pass
        try:
            implant['intelligence'] = {'name': sheet.attributeEnhancers.intelligenceBonus.augmentatorName,
                                       'value': sheet.attributeEnhancers.intelligenceBonus.augmentatorValue}
        except:
            pass
        try:
            implant['willpower'] = {'name': sheet.attributeEnhancers.willpowerBonus.augmentatorName,
                                    'value': sheet.attributeEnhancers.willpowerBonus.augmentatorValue}
        except:
            pass
        try:
            implant['charisma'] = {'name': sheet.attributeEnhancers.charismaBonus.augmentatorName,
                                   'value': sheet.attributeEnhancers.charismaBonus.augmentatorValue}
        except:
            pass
        try:
            character.alliance_name = sheet.allianceName
            character.alliance_id = sheet.allianceID
        except:
            character.alliance_name = ""
            character.alliance_id = 0

        character.corp_name = sheet.corporationName
        character.corp_id = sheet.corporationID
        character.clone_name = sheet.cloneName
        character.clone_skill_points = sheet.cloneSkillPoints
        character.balance = sheet.balance
        character.implant_memory_name = implant['memory']['name']
        character.implant_memory_bonus = implant['memory']['value']
        character.implant_perception_name = implant['perception']['name']
        character.implant_perception_bonus = implant['perception']['value']
        character.implant_intelligence_name = implant['intelligence']['name']
        character.implant_intelligence_bonus = implant['intelligence']['value']
        character.implant_willpower_name = implant['willpower']['name']
        character.implant_willpower_bonus = implant['willpower']['value']
        character.implant_charisma_name = implant['charisma']['name']
        character.implant_charisma_bonus = implant['charisma']['value']

        character.save()

        for skill in sheet.skills:
            try:
                c_skill = CharSkill.objects.get(character=character, skill_id=skill.typeID)
                c_skill.skillpoints = skill.skillpoints
                c_skill.level = skill.level
                c_skill.save()
            except:
                new_skill = CharSkill(character=character,
                                      skill_id=skill.typeID,
                                      skillpoints=skill.skillpoints,
                                      level=skill.level)
                new_skill.save()

        # Set nextupdate to cachedUntil
        timer = APITimer.objects.get(character=character, apisheet='CharacterSheet')
        timer.nextupdate = pytz.utc.localize(datetime.datetime.utcfromtimestamp(sheet._meta.cachedUntil))
        timer.save()
        logger.debug("Finished %s's character sheet update." % character.name)


class ProcessAPISkillTree(PeriodicTask):
    """
    Grab the skill list, iterate it and store to DB
    """

    run_every = datetime.timedelta(hours=24)

    def run(self, **kwargs):
        logger.debug("Importing skilltree...")

        #create our api object
        api = eveapi.EVEAPIConnection()

        #load the skilltree
        skilltree = api.eve.SkillTree()

        #start iterating
        for g in skilltree.skillGroups:
            new_group = SkillGroup(id=g.groupID, name=g.groupName)
            new_group.save()
            for skill in g.skills:
                try:
                    s_primary = skill.requiredAttributes.primaryAttribute
                except:
                    s_primary = ""
                try:
                    s_secondary = skill.requiredAttributes.secondaryAttribute
                except:
                    s_secondary = ""
                if skill.published:
                    published = True
                else:
                    published = False
                new_skill = Skill(id=skill.typeID,
                                  name=skill.typeName,
                                  group=new_group,
                                  published=published,
                                  description=skill.description,
                                  rank=skill.rank,
                                  primary_attribute=s_primary,
                                  secondary_attribute=s_secondary)
                new_skill.save()

        logger.info("Imported %d skill groups from API." % len(skilltree.skillGroups))