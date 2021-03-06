# -*- coding: utf8 -*-

import os
import codecs
import logging
import logging.config
import MySQLdb
import yaml
import multiprocessing
import begin

from relation import Relation
from copy import deepcopy
from ConfigParser import SafeConfigParser
from segtok.segmenter import split_multi
from dependency_graph import DependencyGraph
from word_unit_sequence import WordUnitSequence, Predicate
from entity_linking import EntityLinker
from utils import timeit


class RelationExtractor(object):

    _dependencies = {
        'acomp': 'acomp',
        'aux': 'aux',
        'auxpass': 'auxpass',
        'cc': 'cc',
        'ccomp': 'ccomp',
        'conj:and': 'conj:and',
        'conj:or': 'conj:or',
        'conj:but': 'conj:but',
        'cop': 'cop',
        'dep': 'dep',
        'dobj': 'dobj',
        'neg': 'neg',
        'nn': 'nn',
        'nsubj': 'nsubj',
        'nsubjpass': 'nsubjpass',
        'num': 'num',
        'pcomp': 'pcomp',
        'pobj': 'pobj',
        'prep': 'prep',
        'prt': 'prt',
        'vmod': 'vmod',
        'xcomp': 'xcomp',
    }

    _pos_tags = {
        'cd': 'CD',
        'dt': 'DT',
        'in': 'IN',
        'nn': 'NN',
        'nns': 'NNS',
        'nnp': 'NNP',
        'nnps': 'NNPS',
        'jj': 'JJ',
        'jjr': 'JJR',
        'jjs': 'JJS',
        'prp': 'PRP',
        'prp$': 'PRP$',
        'vb': 'VB',
        'vbd': 'VBD',
        'vbg': 'VBG',
        'vbn': 'VBN',
        'vbp': 'VBP',
        'vbz': 'VBZ',
        'wdt': 'WDT',
        'wp': 'WP',
    }

    _subject_object_pos_blacklist = [
        _pos_tags['wdt'], _pos_tags['dt'], _pos_tags['prp'],
        _pos_tags['jj'], _pos_tags['jjr'], _pos_tags['jjs'],
        _pos_tags['wp'], _pos_tags['in'], _pos_tags['prp$']
    ]

    _prep_blacklist_for_prep_phrases = [
        'including'
    ]

    _conjunction_dependencies = [
        _dependencies['conj:and'],
        _dependencies['conj:or'],
        _dependencies['conj:but'],
        _dependencies['dep']
    ]

    _conjunction_pos_whitelist = [
        _pos_tags['nn'], _pos_tags['nns'], _pos_tags['nnp'], _pos_tags['nnps'],
        _pos_tags['vb'], _pos_tags['vbd'], _pos_tags['vbg'], _pos_tags['vbn'],
        _pos_tags['vbp'], _pos_tags['vbz'], _pos_tags['jj'], _pos_tags['jjr'],
        _pos_tags['jjr']
    ]

    def __init__(self, sentence, parser_server, logger=None, entity_linking_flag=False):
        self._sentence = sentence
        self._parser_server = parser_server
        self.logger = logger if logger else logging.getLogger()
        self.entity_linking_flag = entity_linking_flag
        self._dep_triple_dict = {}
        self._make_dep_triple_dict()
        self._relations = set()

    def _make_dep_triple_dict(self):
        dg = DependencyGraph(self._sentence, self.logger, self._parser_server)
        triples = dg.dep_triples
        dg.print_dep_triples()
        for triple in triples:
            dep = triple[1]
            if dep in self._dependencies.values():
                if dep not in self._dep_triple_dict:
                    self._dep_triple_dict[dep] = []
                self._dep_triple_dict[dep].append({
                    'head': triple[0],
                    'dependent': triple[2]
                })

    @property
    def relations(self):
        return self._relations

    def insert_relation_sql(self, relation, table_name='svo'):
        sentence = self._sentence.replace('"', '')
        return u"""
            INSERT INTO {} (subject_head, subject_nn_head, subject, subject_el, predicate, predicate_canonical,
                            object_head, object_nn_head, object, object_el, sentence)
            VALUES ("{}", "{}", "{}", "{}", "{}", "{}", "{}", "{}", "{}", "{}", "{}");
        """.format(
            table_name,
            relation.subject.head.lemma, relation.subject.nn_head.lemma, relation.subject.lemma, relation.subject_el,
            relation.predicate.lemma, relation.predicate.canonical_form,
            relation.object.head.lemma, relation.object.nn_head.lemma, relation.object.lemma, relation.object_el,
            sentence
        )

    def _print_expansion_debug_info(self, head_word, dep, added):
        self.logger.debug(u'"{}" expanded with {}: "{}"'.format(head_word, dep, added))

    def _get_dependents(self, dependency_relation, head, dependent=None):
        dependents = []
        if dependency_relation in self._dep_triple_dict:
            if dependent:
                dependents = [t['dependent'] for t in self._dep_triple_dict[dependency_relation]
                              if head.index == t['head'].index and dependent.word == t['dependent'].word and
                              t['dependent'].word.isalnum()]
            else:
                dependents = [t['dependent']
                              for t in self._dep_triple_dict[dependency_relation] if head.index == t['head'].index and
                              t['dependent'].word.isalnum()]
        return dependents

    def _get_conjunction(self, head):
        conjunction = [head]
        for dep in self._conjunction_dependencies:
            conj_list = self._get_dependents(dep, head)
            for conj in conj_list:
                if conj.pos in self._conjunction_pos_whitelist:
                    conjunction.append(conj)
                    self._print_expansion_debug_info(head, 'conjunction', conj)
        return conjunction

    def _get_noun_compound(self, head):
        nc = WordUnitSequence()
        nn_list = self._get_dependents(self._dependencies['nn'], head)
        if nn_list:
            for nn in nn_list:
                nc.add_word_unit(nn)
                self._print_expansion_debug_info(head, 'noun compound', nn)
        return nc

    def _get_num_modifier(self, head):
        num_mod = WordUnitSequence()
        num_list = self._get_dependents(self._dependencies['num'], head)
        if num_list:
            for num in num_list:
                num_mod.add_word_unit(num)
                self._print_expansion_debug_info(head, 'numeric modifier', num)
        return num_mod

    def _get_neg_modifier(self, head):
        neg_mod = WordUnitSequence()
        neg_list = self._get_dependents(self._dependencies['neg'], head)
        if neg_list and neg_list[0].pos == self._pos_tags['dt']:
            neg_mod.add_word_unit(neg_list[0])
            self._print_expansion_debug_info(head, 'negation', neg_list[0])
        return neg_mod

    def _get_prep_phrases(self, head):
        prep_phrases = []
        prep_list = self._get_dependents(self._dependencies['prep'], head)
        if prep_list:
            for prep in prep_list:
                if prep.word.lower() not in self._prep_blacklist_for_prep_phrases:
                    # Ignore those prepositions that are far away from the head
                    if abs(prep.index - head.index) < 3:
                        # Look for pobj
                        obj_list = self._get_dependents(self._dependencies['pobj'], prep)
                        if obj_list:
                            for obj in obj_list:
                                if not self._head_extracting_condition(obj, pos=True):
                                    continue
                                obj_conjunction = self._get_conjunction(obj)
                                for o in obj_conjunction:
                                    obj_seq = self._expand_head_word(o)
                                    if obj_seq:
                                        obj_seq.add_word_unit(prep)
                                        prep_phrase = WordUnitSequence()
                                        prep_phrase.extend(obj_seq)
                                        prep_phrase.head = o
                                        prep_phrase.nn_head = obj_seq.nn_head
                                        if prep_phrase:
                                            self._print_expansion_debug_info(head, 'prep phrase', prep_phrase)
                                            prep_phrases.append(prep_phrase)
        return prep_phrases

    def _get_vmod_phrase(self, head):
        vmod_phrase = WordUnitSequence()
        vmod_list = self._get_dependents(self._dependencies['vmod'], head)
        if vmod_list:
            for vmod in vmod_list:
                predicate_object = self._get_predicate_object(vmod)
                if predicate_object:
                    predicate, object = predicate_object[0]
                    vmod_phrase.extend(predicate)
                    vmod_phrase.extend(object)
                    self._print_expansion_debug_info(head, 'vmod', vmod_phrase)
        return vmod_phrase

    def _expand_head_word(self, head):

        def _clean(word_unit_seq):
            # If the sequence is a single letter, ignore it
            if len(word_unit_seq) == 1 and len(word_unit_seq[0]) == 1:
                word_unit_seq = None
            # If the head of the sequence is a number, ignore it
            if word_unit_seq.head.pos == self._pos_tags['cd']:
                word_unit_seq = None
            return word_unit_seq

        expansion = WordUnitSequence(head, head)
        # Find out if the head is in a compound noun
        noun_compound = self._get_noun_compound(head)
        expansion.extend(noun_compound)
        expansion.nn_head = deepcopy(expansion)
        # # Find out if there is any numeric modifier
        # num_mod = self._get_num_modifier(head)
        # expansion.extend(num_mod)
        # Find out if there is any negation
        neg_mod = self._get_neg_modifier(head)
        expansion.extend(neg_mod)
        # Find out if the head has pobj phrase
        pobj_phrases = self._get_prep_phrases(head)
        if pobj_phrases:
            pobj_phrase = pobj_phrases[0]
            expansion.extend(pobj_phrase)
            # Transfer the head in the pattern "<num> of <noun>" from "<num>" to "<noun>"
            if (head.pos == self._pos_tags['cd'] or head.word.isdigit()) and pobj_phrase[0].word == 'of':
                expansion.head = pobj_phrase.head
                expansion.nn_head = pobj_phrase.nn_head
        # Find out if the head has vmod phrase
        vmod_phrase = self._get_vmod_phrase(head)
        expansion.extend(vmod_phrase)
        # Cleaning
        expansion = _clean(expansion)
        return expansion

    def _expand_predicate(self, head):

        def __expand_predicate(pred_head):
            predicate = Predicate()
            dep_list = [
                self._dependencies['aux'],
                self._dependencies['auxpass'],
                self._dependencies['neg'],
                self._dependencies['prt'],
                self._dependencies['cop']
            ]
            for dep in dep_list:
                dep_wn = self._get_dependents(dep, pred_head)
                if dep_wn:
                    for d in dep_wn:
                        predicate.add_word_unit(d)
                        self._print_expansion_debug_info(pred_head, dep, d)
                        if dep == self._dependencies['neg']:
                            predicate.negation.append(d)
                        if dep == self._dependencies['aux']:
                            predicate.auxiliary.append(d)
            return predicate

        predicates = []
        predicate = Predicate(head, head)
        # Find out if there is any aux, auxpass, and neg
        expanded_pred = __expand_predicate(head)
        predicate.extend(expanded_pred)
        predicate.negation = expanded_pred.negation
        predicate.auxiliary = expanded_pred.auxiliary
        # Find out if there is any xcomp
        xcomp_list = self._get_dependents(self._dependencies['xcomp'], head)
        if xcomp_list:
            # Get xcomp conjunction first
            xcomp_list_len = len(xcomp_list)
            for i in xrange(xcomp_list_len):
                xcomp_list.extend(self._get_conjunction(xcomp_list[i]))
            for xcomp in xcomp_list:
                if xcomp.pos.startswith(self._pos_tags['vb']):
                    # If the xcomp doesn't immediately follow its head, separate them
                    if xcomp.index - predicate.head.index > 3:
                        pred = Predicate(xcomp, xcomp)
                        pred.extend(__expand_predicate(xcomp))
                        # Remove "to" preceding the comp
                        for wn in pred.sequence:
                            if wn.index < xcomp.index and wn.word == 'to':
                                pred.remove_word_unit(wn)
                        # Also add the head to predicates
                        predicates.append(predicate)
                    else:
                        pred = deepcopy(predicate)
                        pred.add_word_unit(xcomp)
                        pred.extend(__expand_predicate(xcomp))
                    self._print_expansion_debug_info(head, 'xcomp', xcomp)
                    predicates.append(pred)
                elif xcomp.pos.startswith(self._pos_tags['nn']):
                    pred = deepcopy(predicate)
                    pred.add_word_unit(xcomp)
                    pred.extend(__expand_predicate(xcomp))
                    self._print_expansion_debug_info(head, 'xcomp', xcomp)
                    predicates.append(pred)
        else:
            predicates.append(predicate)
        return predicates

    def _get_predicate_object(self, pred_head):
        predicate_object = []
        predicates = self._expand_predicate(pred_head)
        for predicate in predicates:
            dobj_flag, acomp_flag, pobj_flag = False, False, False
            for ind, pred in predicate:
                # Look for direct object
                obj_list = self._get_dependents(self._dependencies['dobj'], pred)
                if obj_list:
                    for obj in obj_list:
                        if not self._head_extracting_condition(obj, pos=True):
                            continue
                        obj_conjunction = self._get_conjunction(obj)
                        for o in obj_conjunction:
                            expanded_obj = self._expand_head_word(o)
                            if expanded_obj:
                                object = WordUnitSequence()
                                object.extend(expanded_obj)
                                object.head = o
                                object.nn_head = expanded_obj.nn_head
                                dobj_flag = True
                                predicate_object.append((predicate, object))
                    continue
                # Look for adjective compliment
                acomp_list = self._get_dependents(self._dependencies['acomp'], pred)
                if acomp_list:
                    for acomp in acomp_list:
                        acomp_prep_phrases = self._get_prep_phrases(acomp)
                        for acomp_prep_phrase in acomp_prep_phrases:
                            if len(acomp_prep_phrase) > 1:
                                object = WordUnitSequence()
                                object.extend(WordUnitSequence(acomp_prep_phrase[1:]))
                                object.head = acomp_prep_phrase.head
                                object.nn_head = acomp_prep_phrase.nn_head
                                # Make a copy of predicate in case it gets expanded
                                predicate_copy = deepcopy(predicate)
                                # Merge the acomp and prep into the predicate
                                predicate_copy.add_word_unit(acomp)
                                predicate_copy.add_word_unit(acomp_prep_phrase[0])
                                acomp_flag = True
                                predicate_object.append((predicate_copy, object))
                    continue
                # Look for prepositional objects
                prep_phrases = self._get_prep_phrases(pred)
                for prep_phrase in prep_phrases:
                    if len(prep_phrase) > 1:
                        object = WordUnitSequence()
                        object.extend(WordUnitSequence(prep_phrase[1:]))
                        object.head = prep_phrase.head
                        object.nn_head = prep_phrase.nn_head
                        # Make a copy of predicate in case it gets expanded
                        predicate_copy = deepcopy(predicate)
                        # Merge the prep into the predicate
                        predicate_copy.add_word_unit(prep_phrase[0])
                        pobj_flag = True
                        predicate_object.append((predicate_copy, object))
            # Also return the predicate if it has no object in case it is a conjunction of other predicates.
            if not dobj_flag and not acomp_flag and not pobj_flag:
                predicate_object.append((predicate, None))
        return predicate_object

    def _head_extracting_condition(self, head, pos=False):
        flag = head.word.isalnum()
        if pos:
            flag = flag and head.pos not in self._subject_object_pos_blacklist
        return flag

    def extract_spo(self):

        def link_entity(linker, query, context):
            if not isinstance(context, list):
                context = context.split()
            query_arr = [query]
            for w in [wn for wn in context if not wn == query]:
                query_arr.append(w)
            return linker.link(query_arr)

        linker = EntityLinker(self.logger) if self.entity_linking_flag else None
        dependencies = [self._dependencies['nsubj'], self._dependencies['nsubjpass']]
        for dep in dependencies:
            if dep in self._dep_triple_dict:
                self._extract_spo(dep)
                if self.entity_linking_flag:
                    for relation in self._relations:
                        subj_head = relation.subject.head
                        if subj_head:
                            relation.subject_el = link_entity(linker, subj_head.lemma, relation.subject.lemma)
                        obj_head = relation.object.head
                        if obj_head:
                            relation.object_el = link_entity(linker, obj_head.lemma, relation.object.lemma)

    def _extract_spo(self, dependency):
        for triple in self._dep_triple_dict[dependency]:
            head = triple['head']
            dependent = triple['dependent']
            if not self._head_extracting_condition(head) \
               or not self._head_extracting_condition(dependent, pos=True):
                continue
            head_conjunction = self._get_conjunction(head)
            dependent_conjunction = self._get_conjunction(dependent)
            for dependent in dependent_conjunction:
                # The subject is the dependent
                subject = self._expand_head_word(dependent)
                if subject:
                    for head in head_conjunction:
                        if head.pos.startswith(self._pos_tags['vb']):
                            # The predicate is the head
                            for predicate, object in self._get_predicate_object(head):
                                if predicate and object:
                                    self.relations.add(Relation(subject, predicate, object))
                                # Deal with conjunct predicates that have no objects.
                                else:
                                    for h in [h for h in head_conjunction if not h == head and h.pos == head.pos]:
                                        for p, o in self._get_predicate_object(h):
                                            if p and o:
                                                self.relations.add(
                                                    Relation(subject, Predicate(predicate.head, predicate.head), o))
                        elif head.pos.startswith(self._pos_tags['nn']):
                            pred_list = self._get_dependents(self._dependencies['cop'], head)
                            if pred_list:
                                predicates = self._expand_predicate(pred_list[0])
                                for predicate in predicates:
                                    object = self._expand_head_word(head)
                                    if predicate and object:
                                        self.relations.add(Relation(subject, predicate, object))
                        elif head.pos.startswith(self._pos_tags['jj']):
                            pred_list = self._get_dependents(self._dependencies['cop'], head)
                            if pred_list:
                                for predicate, object in self._get_predicate_object(head):
                                    if predicate and object:
                                        # predicate.add_word_unit(pred_list[0])
                                        self.relations.add(Relation(subject, predicate, object))


@begin.subcommand
def batch_extraction(parser_port, dataset, dataset_no, mysql_db):
    with open('config/logging_config.yaml') as f:
        logging.config.dictConfig(yaml.load(f))
    logger = logging.getLogger('batch_relation_extraction')

    parser_server = 'http://127.0.0.1:{}'.format(str(parser_port))

    mysql_config = SafeConfigParser()
    mysql_config.read('config/mysql_config.ini')
    mysql_config = {
        'host': mysql_config.get('MySQL', 'host'),
        'user': mysql_config.get('MySQL', 'user'),
        'passwd': mysql_config.get('MySQL', 'passwd'),
        'db': mysql_db,
        'charset': mysql_config.get('MySQL', 'charset'),
        'use_unicode': True
    }
    conn = MySQLdb.connect(**mysql_config)
    cur = conn.cursor()

    data_subdir = 'preprocessed_organized'
    data_dir = 'data/{}/{}/{}'.format(dataset, data_subdir, dataset_no)
    for root, _, files in os.walk(data_dir):
        for fn in files:
            if fn.endswith('.txt'):
                data_file = os.path.join(root, fn)
                f_in = codecs.open(data_file, encoding='utf-8')
                # output_filename = os.path.join(root, fn).replace('/preprocessed/', '/extractions/')
                # f_out = codecs.open(output_filename, 'w', encoding='utf-8')
                for line in f_in:
                    sent = line.strip()
                    if sent:
                        logger.info(u'{}: {}'.format(data_file, sent))
                        # f_out.write(u'{}\n'.format(sent))
                        try:
                            extractor = RelationExtractor(sent, parser_server, logger, entity_linking_flag=False)
                            extractor.extract_spo()
                        except:
                            logger.error(u'Failed to extract relations from: {}.'.format(sent), exc_info=True)
                        else:
                            for relation in extractor.relations:
                                logger.info(u'RELATION: {}'.format(relation))
                                # f_out.write(u'{} [{}]\n'.format(relation, relation.canonical_form))
                                if mysql_db:
                                    try:
                                        cur.execute(extractor.insert_relation_sql(relation))
                                        conn.commit()
                                    except MySQLdb.Error as e:
                                        try:
                                            logger.error(u'MySQL Error [{}]: {}'.format(e.args[0], e.args[1]),
                                                         exc_info=True)
                                        except IndexError:
                                            logger.error(u'MySQL Error: {}'.format(str(e)), exc_info=True)
                            # f_out.write('\n')

                f_in.close()
                # f_out.close()

                done_subdir = 'extraction_done'
                done_filename = data_file.replace('/{}/'.format(data_subdir), '/{}/'.format(done_subdir))
                if not os.path.exists(os.path.dirname(done_filename)):
                    os.makedirs(os.path.dirname(done_filename))
                os.rename(data_file, done_filename)

    cur.close()
    conn.close()


@begin.subcommand
def single_extraction(sentences):
    with open('config/logging_config.yaml') as f:
        logging.config.dictConfig(yaml.load(f))
    logger = logging.getLogger('single_relation_extraction')

    parser_server = 'http://127.0.0.1:8084'

    for sent in split_multi(sentences):
        sent = sent.strip()
        if sent:
            logger.debug(u'SENTENCE: {}'.format(sent))
            try:
                extractor = RelationExtractor(sent, parser_server, logger, entity_linking_flag=False)
            except:
                logger.error(u'Failed to parse the sentence', exc_info=True)
            else:
                extractor.extract_spo()
                for relation in extractor.relations:
                    logger.debug(u'SUBJECT HEAD: {}'.format(relation.subject.head))
                    logger.debug(u'SUBJECT NN HEAD: {}'.format(relation.subject.nn_head))
                    if extractor.entity_linking_flag:
                        logger.debug(u'SUBJECT EL: {}'.format(relation.subject_el))
                    logger.debug(u'OBJECT HEAD: {}'.format(relation.object.head))
                    logger.debug(u'OBJECT NN HEAD: {}'.format(relation.object.nn_head))
                    if extractor.entity_linking_flag:
                        logger.debug(u'OBJECT EL: {}'.format(relation.object_el))
                    logger.debug(u'RELATION LEMMA: {}'.format(relation.lemma))
                    logger.debug(u'RELATION CANONICAL: {}'.format(relation.canonical_form))


@begin.subcommand
def organize_data_folder(dataset, sub_folder_no):
    with open('config/logging_config.yaml') as f:
        logging.config.dictConfig(yaml.load(f))
    logger = logging.getLogger('single_relation_extraction')

    data_dir = 'data/{}/preprocessed'.format(dataset)
    file_no = 0
    for root, _, files in os.walk(data_dir):
        for fn in files:
            if fn.endswith('.txt'):
                filename = os.path.join(root, fn)
                new_filename = filename.replace(
                    '/preprocessed/', '/preprocessed_organized/{}/'.format(str(file_no % int(sub_folder_no))))
                logger.info('{} -> {}'.format(filename, new_filename))
                if not os.path.exists(os.path.dirname(new_filename)):
                    os.makedirs(os.path.dirname(new_filename))
                os.rename(filename, new_filename)
                file_no += 1


@begin.start
def main():
    pass

if begin.start():
    pass
