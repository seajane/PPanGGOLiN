#!/usr/bin/env python3
# coding:utf-8


# default libraries
import argparse
import tempfile
import time
import logging

#installed libraries
from tqdm import tqdm
import networkx as nx

# local libraries
from ppanggolin.formats import checkPangenomeInfo
from ppanggolin.utils import mkOutdir, restricted_float, add_gene, connected_components
from ppanggolin.pangenome import Pangenome
from ppanggolin.align.alignOnPang import get_prot2pang
from ppanggolin.geneFamily import GeneFamily


def _write_graph(g):
    import matplotlib.pyplot as plt

    labelnodes = {node: node.ID for node in g.nodes}
    labeledges = {edge: f'({str(edge[0].ID)}, {str(edge[1].ID)})' for edge in g.edges}
    plt.figure(figsize=(24, 24))
    p = nx.spring_layout(g)
    nx.draw(g, p, with_labels=True, labels=labelnodes)
    nx.draw_networkx_edge_labels(g, p, edge_labels=labeledges, font_color='red')
    plt.savefig("test.png")


class CC:
    def __init__(self, ID, families=None):
        self.ID = ID
        self.families = set()
        if families is not None:
            if not all(isinstance(fam, GeneFamily) for fam in families):
                raise Exception(
                    f"You provided elements that were not GeneFamily object. CC are only made of GeneFamily")
            self.families |= set(families)

    def add_family(self, family):
        if not isinstance(family, GeneFamily):
            raise Exception("You did not provide a GenFamily object. Modules are only made of GeneFamily")
        self.families.add(family)

# def checkPangenomeFormerCC(pangenome, force):
#     """ checks pangenome status and .h5 files for former modules, delete them if allowed or raise an error """
#     if pangenome.status["modules"] == "inFile" and force == False:
#         raise Exception("You are trying to detect modules on a pangenome which already has predicted modules. If you REALLY want to do that, use --force (it will erase modules previously predicted).")
#     elif pangenome.status["modules"] == "inFile" and force == True:
#         ErasePangenome(pangenome, modules = True)


def search_cc_in_pangenome(pangenome, proteins, output, tmpdir,  transitive=4, identity=0.5, coverage=0.8, jaccard=0.85,
                           no_defrag=False, cpu=1, force=False, show_bar=True):
    #check statuses and load info
    # checkPangenomeFormerCC(pangenome, force)
    checkPangenomeInfo(pangenome, needFamilies=True, needAnnotations=True)

    #Alignment of proteins on pangenome's families
    new_tmpdir = tempfile.TemporaryDirectory(dir=tmpdir)
    prot2pan = get_prot2pang(pangenome, proteins, output, new_tmpdir, cpu, no_defrag, identity, coverage)[-1]
    new_tmpdir.cleanup()

    #Compute the graph with transitive closure size provided as parameter
    start_time = time.time()
    logging.getLogger().info("Building the graph...")
    g = compute_cc_graph(alignment=prot2pan, t=transitive, show_bar=show_bar)
    logging.getLogger().info(f"Took {round(time.time() - start_time,2)} seconds to build the graph to find commont component in")
    logging.getLogger().info(f"There are {nx.number_of_nodes(g)} nodes and {nx.number_of_edges(g)} edges")

    #extract the modules from the graph
    common_components = compute_cc(g, jaccard)

    print([elem.ID for elem in common_components])
    # write_graph(g)
    families = set()
    for cc in common_components:
        families |= cc.families

    logging.getLogger().info(f"There are {len(families)} families among {len(common_components)} modules")
    logging.getLogger().info(f"Computing common components took {round(time.time() - start_time,2)} seconds")


def compute_cc_graph(alignment, t, show_bar=True):
    g = nx.Graph()
    for protein, gene_family in tqdm(alignment.items(), unit="proteins", disable=not show_bar):
        # print(protein, gene_family)
        for gene in gene_family.genes:
            contig = gene.contig._genes_position  # TODO create method to extract
            pos_left, in_context_left, pos_right, in_context_right = extract_gene_context(gene, contig, alignment, t)
            if in_context_left or in_context_right:
                # print(contig[pos_left:pos_right + 1], len(contig[pos_left:pos_right + 1]), gene.position, pos_left,
                #       pos_right)
                for env_gene in contig[pos_left:pos_right + 1]:
                    g.add_node(env_gene.family)
                    add_gene(g.nodes[env_gene.family], gene, fam_split=False)
                    pos = env_gene.position + 1
                    while pos <= pos_right and pos < len(contig):
                        if env_gene.family != contig[pos].family:
                            g.add_edge(env_gene.family, contig[pos].family)
                            edge = g[env_gene.family][contig[pos].family]
                            add_gene(edge, env_gene)
                            add_gene(edge, contig[pos])
                        pos += 1
    return g


def extract_gene_context(gene, contig, alignment, t=4):
    # print(gene.contig._genes_position[gene.position-t:gene.position+t+1])
    pos_left, pos_right = (max(0, gene.position - t),
                           gene.position + t)  # Gene position to compare family
    in_context_left, in_context_right = (False, False)
    while pos_left < gene.position and not in_context_left:
        if contig[pos_left].family in alignment.values():
            in_context_left = True
        else:
            pos_left += 1

    while pos_right < gene.position and not in_context_right:
        if contig[pos_right].family in alignment.values():
            in_context_right = True
        else:
            pos_right -= 1

    return pos_left, in_context_left, pos_right, in_context_right


def compute_cc(g, jaccard=0.85):
    cc = set()
    c = 0
    for comp in connected_components(g, removed=set(), weight=0.85):
        if not any(fam.namedPartition == "persistent" for fam in comp):
            cc.add(CC(ID=c, families=comp))
            c += 1
    return cc


def launch(args):
    mkOutdir(args.output, args.force)
    pangenome = Pangenome()
    pangenome.addFile(args.pangenome)
    search_cc_in_pangenome(pangenome=pangenome, proteins=args.proteins, output=args.output, identity=args.identity,
                           coverage=args.coverage, jaccard=args.jaccard, transitive=args.transitive, tmpdir=args.tmpdir,
                           no_defrag=args.no_defrag, cpu=args.cpu,  force=args.force, show_bar=args.show_prog_bars)
    # writePangenome(pangenome, pangenome.file, args.force, show_bar=args.show_prog_bars)

def contextSubparser(sub_parser):
    """
    Parser arguments specific to align command

    :param sub_parser : sub_parser for align command
    :type sub_parser : argparse._SubParsersAction

    :return : parser arguments for align command
    :rtype : argparse.ArgumentParser
    """
    parser = sub_parser.add_parser("context", formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    required = parser.add_argument_group(title="Required arguments",
                                         description="All of the following arguments are required :")
    required.add_argument('-p', '--pangenome', required=True, type=str, help="The pangenome .h5 file")
    required.add_argument('-o', '--output', required=True, type=str,
                          help="Output directory where the file(s) will be written")
    required.add_argument('-P', '--proteins', required=True, type=str, help="Fasta with all proteins of interest")

    optional = parser.add_argument_group(title = "Optional arguments")
    optional.add_argument('--no_defrag', required=False, action="store_true",
                          help="DO NOT Realign gene families to link fragments with"
                               "their non-fragmented gene family. (default: False)")
    optional.add_argument('--identity', required=False, type=float, default=0.5,
                          help="min identity percentage threshold")
    optional.add_argument('--coverage', required=False, type=float, default=0.8,
                          help="min coverage percentage threshold")
    optional.add_argument("-t", "--transitive", required=False, type=int, default=4,
                          help="Size of the transitive closure used to build the graph. This indicates the number of "
                               "non related genes allowed in-between two related genes. Increasing it will improve "
                               "precision but lower sensitivity a little.")
    optional.add_argument("-j", "--jaccard", required=False, type=restricted_float, default=0.85,
                          help="minimum jaccard similarity used to filter edges between gene families. Increasing it "
                               "will improve precision but lower sensitivity a lot.")

    return parser
