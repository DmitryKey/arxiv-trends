#!/usr/bin/env python
# -*- coding: utf-8 -*-
# see license https://github.com/DerwenAI/arxiv-trends#license-and-copyright

"""
arxiv-trends
"""

from collections import defaultdict
import itertools
import pathlib
import sys
import typing
import urllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as et

from icecream import ic
import dateutil.tz
import kglab
import numpy as np
import matplotlib.pyplot as plt
import pandas as pd
import pytextrank
import rdflib
import spacy
import typer


APP = typer.Typer()


class Trends:
    """
Analyze trends among papers published on arXiv.
    """
    NS = {
        "atom":       "http://www.w3.org/2005/Atom",
        "bibo":       "http://purl.org/ontology/bibo/",
        "cito":       "http://purl.org/spar/cito/",
        "dct":        "http://purl.org/dc/terms/",
        "derw":       "https://derwen.ai/ns/v1#",
        "foaf":       "http://xmlns.com/foaf/0.1/",
        "lcsh":       "http://id.loc.gov/authorities/subjects/",
        "madsrdf":    "http://www.loc.gov/mads/rdf/v1#",
        "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
        "owl":        "http://www.w3.org/2002/07/owl#",
        "prov":       "http://www.w3.org/ns/prov#",
        "rdf":        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs":       "http://www.w3.org/2000/01/rdf-schema#",
        "schema":     "http://schema.org/",
        "sh":         "http://www.w3.org/ns/shacl#",
        "skos":       "http://www.w3.org/2004/02/skos/core#",
        "wd":         "http://www.wikidata.org/entity/",
        "xsd":        "http://www.w3.org/2001/XMLSchema#",
    }

    API_BASE = "http://export.arxiv.org/api/query?"


    def __init__ (
        self,
        kg_path: str = "arxiv.ttl",
        ):
        """
Constructor.
        """
        self.kg = kglab.KnowledgeGraph(namespaces=self.NS)
        self.kg_path = pathlib.Path(kg_path)
        self.topics: typing.Dict[str, rdflib.Node] = {}
        self.load_kg()


    def load_kg (
        self,
        ) -> None:
        """
Load the previous definitions from a serialized KG and initialize the
topics lookup.
        """
        self.topics = {}
        self.kg.load_rdf(self.kg_path)

        sparql = """
SELECT ?entry ?label
WHERE {
 ?entry a derw:Topic .
 ?entry skos:prefLabel ?label
}
        """

        for node, topic in self.kg.query(sparql):
            self.topics[topic.toPython()] = node


    def save_kg (
        self,
        ) -> None:
        """
Serialize the updated KG to a file.
        """
        self.kg.save_rdf(self.kg_path)


    def lookup_author (
        self,
        name: str,
        ) -> rdflib.URIRef:
        """
Lookup an author by name, creating a node in the KG if it doesn't
already exist.

    returns:
author node
        """
        uri = self.kg.get_ns("derw") + "author_" + name.replace(" ", "_").lower()
        node = rdflib.URIRef(uri)
        p = self.kg.get_ns("rdf").type
        o = self.kg.get_ns("derw").Author

        if (node, p, o) not in self.kg.rdf_graph():
            self.kg.add(node, p, o)
            self.kg.add(node, self.kg.get_ns("foaf").name, rdflib.Literal(name, lang=self.kg.language))

        return node


    def parse_entry (
        self,
        entry: et.Element,
        ):
        """
Parse the XML from one entry in an Atom feed, and add it to the KG.

    returns:
href and date of the parsed results
        """
        href = entry.find("atom:link[@title='pdf']", self.NS).attrib["href"]
        date = entry.find("atom:published", self.NS).text[:10]
        title = entry.find("atom:title", self.NS).text
        abstract = entry.find("atom:summary", self.NS).text.replace("\n", " ").strip()

        # lookup the specified article in the KG, and create a node if
        # it doesn't already exist
        node = rdflib.URIRef(href)
        p = self.kg.get_ns("rdf").type
        o = self.kg.get_ns("bibo").Article

        if (node, p, o) not in self.kg.rdf_graph():
            self.kg.add(node, p, o)
            self.kg.add(node, self.kg.get_ns("dct").Date, self.kg.encode_date(date, [dateutil.tz.gettz("UTC")]))
            self.kg.add(node, self.kg.get_ns("dct").title, rdflib.Literal(title, lang=self.kg.language, normalize=False))
            self.kg.add(node, self.kg.get_ns("dct").abstract, rdflib.Literal(abstract, lang=self.kg.language))

            # add author list
            for author in entry.findall("atom:author/atom:name", self.NS):
                self.kg.add(node, self.kg.get_ns("bibo").authorList, self.lookup_author(author.text))

        return node, date


    @classmethod
    def format_query (
        cls,
        query: str,
        start: int,
        max_results: int,
        ) -> str:
        """
Format a URL to search arXiv via its API, based on the given search 
criteria.

    returns:
query URL
        """
        params: dict = {
            "search_query": "all:" + query,
            "start": start,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }

        return urllib.parse.urlencode(params, safe=":")


    def arxiv_api (
        self,
        query: str,
        min_date: str,
        *,
        max_items: int = 1,
        page_items: int = 1,
        ) -> typing.Iterable:
        """
Access the arXiv API based on the given search criteria, parse the XML
results (Atom feed), then update the KG to represent any new entries.

    yields:
`(date, href)` tuple for each search hit within the criteria
        """
        start_index = 0
        max_index = max_items

        while (start_index < max_index):
            # prepare the API query
            url = self.API_BASE + self.format_query(query, start_index, page_items)
            handle = urllib.request.urlopen(url)

            xml = handle.read().decode("utf-8")
            #print(xml)

            # track the API results paging
            root = et.fromstring(xml)
            total_results = int(root.findall("opensearch:totalResults", self.NS)[0].text)
            start_index = int(root.findall("opensearch:startIndex", self.NS)[0].text)
            page_items = int(root.findall("opensearch:itemsPerPage", self.NS)[0].text)

            print("---")
            ic(total_results)
            ic(start_index)
            ic(page_items)

            # parse each entry
            for entry in root.findall("atom:entry", self.NS):
                node, date = self.parse_entry(entry)
                yield date, node

                if date < min_date:
                    return

            # iterate to the next page of results
            max_index = min(max_items, total_results)
            start_index += page_items

        return


######################################################################
## commands

@APP.command()
def cmd_query (
    query,
    min_date = "2021-06-15",
    ):
    """
Query the arXiv API for the given search.
    """
    trends = Trends()

    # search parameters
    max_items = 1000
    page_items = 100

    # get metadata for the matching articles
    hit_iter = trends.arxiv_api(
        " AND ".join(query.split(" ")),
        min_date,
        max_items=max_items,
        page_items=page_items,
    )

    for date, node in hit_iter:
        trends.kg.add(node, trends.kg.get_ns("derw").fromQuery, trends.topics[query])
        # TODO: what if query new?
        print(query, date, node)

    # persist the metadata
    trends.save_kg()


@APP.command()
def cmd_extract (
    max_phrase = 10,
    ):
    """
Extract the entities fron each article.
    """
    trends = Trends()

    # prepare the NLP pipeline
    nlp = spacy.load("en_core_web_sm")
    nlp.add_pipe("textrank")

    sparql = """
SELECT ?article ?title ?abstract
WHERE {
 ?article a bibo:Article .
 ?article dct:title ?title .
 ?article dct:abstract ?abstract
}
    """

    # run the pipeline for each article
    for node, title, abstract in trends.kg.query(sparql):
        text = title.toPython() + ".  " + abstract.toPython()
        doc = nlp(text)

        for phrase in itertools.islice(doc._.phrases, max_phrase):
            entity_label = " ".join(phrase.text.replace("\n", " ").strip().split()).lower()
            print(node, round(phrase.rank, 3), phrase.count, entity_label)


@APP.command()
def cmd_analyze (
    csv_file: str = "arxiv.csv",
    ):
    """
Analyze the article trends.
    """
    trends = Trends()

    sparql = """
SELECT ?article ?date ?label
WHERE {
 ?article a bibo:Article .
 ?article dct:Date ?date .
 ?article derw:fromQuery ?topic .
 ?topic skos:prefLabel ?label
}
    """

    # run the pipeline for each article
    df = pd.DataFrame([
        {
            "topic": topic.toPython(),
            "date": date.toPython(),
            "counts": 0,
        }
        for article, date, topic in trends.kg.query(sparql)
    ]).groupby(["topic", "date"]).count()

    # serialize to a CSV file
    path = pathlib.Path(csv_file)
    df.to_csv(path)


if __name__ == "__main__":
    APP()

    # simply reminders...
    query_list = [
        "knowledge graph",
        "graph database",
        "graph algorithm",
        "graph neural networks",
        "graph embedding",
        ]
