#!/usr/bin/env python3
# coding:utf-8

# default libraries
import argparse
import logging
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
import os
from pathlib import Path
import tempfile
import time
from typing import List, Set, Tuple, Iterable

# installed libraries
from tqdm import tqdm
from ppanggolin.annotate.synta import (annotate_organism, read_fasta, get_dna_sequence,
                                       init_contig_counter, contig_counter)
from ppanggolin.pangenome import Pangenome
from ppanggolin.genome import Organism, Gene, RNA, Contig
from ppanggolin.utils import read_compressed_or_not, mk_file_name, detect_filetype, check_input_files
from ppanggolin.formats import write_pangenome

ctg_counter = contig_counter


def check_annotate_args(args: argparse.Namespace):
    """Check That the given arguments are usable

    :param args: All arguments provide by user

    :raise Exception:
    """
    if args.fasta is None and args.anno is None:
        raise Exception("You must provide at least a file with the --fasta option to annotate from sequences, "
                        "or a file with the --gff option to load annotations from.")

    if hasattr(args, "fasta") and args.fasta is not None:
        check_input_files(args.fasta, True)

    if hasattr(args, "anno") and args.anno is not None:
        check_input_files(args.anno, True)


def create_gene(org: Organism, contig: Contig, gene_counter: int, rna_counter: int, gene_id: str, dbxref: Set[str],
                start: int, stop: int, strand: str, gene_type: str, position: int = None, gene_name: str = "",
                product: str = "", genetic_code: int = 11, protein_id: str = ""):
    """
    Create a Gene object and associate to contig and Organism

    :param org: Organism to add gene
    :param contig: Contig to add gene
    :param gene_counter: Gene counter to name gene
    :param rna_counter: RNA counter to name RNA
    :param gene_id: local identifier
    :param dbxref: cross-reference to external DB
    :param start: Gene start position
    :param stop: Gene stop position
    :param strand: gene strand association
    :param gene_type: gene type
    :param position: position in contig
    :param gene_name: Gene name
    :param product: Function of gene
    :param genetic_code: Genetic code used
    :param protein_id: Protein identifier
    """
    if any('MaGe' or 'SEED' in dbref for dbref in dbxref):
        if gene_name == "":
            gene_name = gene_id
        for val in dbxref:
            if 'MaGe' in val:
                gene_id = val.split(':')[1]
                break
            if 'SEED' in val:
                gene_id = val.split(':')[1]
                break
    if gene_type == "CDS":
        if gene_id == "":
            gene_id = protein_id
            # on rare occasions, there are no 'locus_tag' from downloaded .gbk file.
            # So we use the protein_id field instead. (which is not supposed to be unique,
            # but was when cases like this were encountered)

        new_gene = Gene(org.name + "_CDS_" + str(gene_counter).zfill(4))
        new_gene.fill_annotations(start=start, stop=stop, strand=strand, gene_type=gene_type, name=gene_name,
                                  position=position, product=product, local_identifier=gene_id,
                                  genetic_code=genetic_code)
        contig.add(new_gene)
    else:  # if not CDS, it is RNA
        new_gene = RNA(org.name + f"_{gene_type}_" + str(rna_counter).zfill(4))
        new_gene.fill_annotations(start=start, stop=stop, strand=strand, gene_type=gene_type, name=gene_name,
                                  product=product)
        contig.add_rna(new_gene)
    new_gene.fill_parents(org, contig)


def read_org_gbff(organism_name: str, gbff_file_path: Path, circular_contigs: List[str],
                  pseudo: bool = False) -> Tuple[Organism, bool]:
    """
    Read a GBFF file and fills Organism, Contig and Genes objects based on information contained in this file

    :param organism_name: Organism name
    :param gbff_file_path: Path to corresponding GBFF file
    :param circular_contigs: list of contigs
    :param pseudo: Allow to read pseudogenes

    :return: Organism complete and true for sequence in file
    """
    global ctg_counter

    organism = Organism(organism_name)
    logging.getLogger("PPanGGOLiN").debug(f"Extracting genes information from the given gbff {gbff_file_path.name}")
    # revert the order of the file, to read the first line first.
    lines = read_compressed_or_not(gbff_file_path).readlines()[::-1]
    gene_counter = 0
    rna_counter = 0
    while len(lines) != 0:
        line = lines.pop()

        # ignore line if empty
        if line.rstrip() == "":
            continue
        
        # beginning of contig
        is_circ = False
        contig_id = None
        contig_len = None
        if line.startswith('LOCUS'):
            if "CIRCULAR" in line.upper():
                # this line contains linear/circular word telling if the dna sequence is circularized or not
                is_circ = True
            # TODO maybe it could be a good thing to add a elif for linear
            #  and if circular or linear are not found raise a warning
            contig_id = line.split()[1]
            contig_len = int(line.split()[2])
            # If contig_id is not specified in VERSION afterward like with Prokka, in that case we use the one in LOCUS
            while not line.startswith('FEATURES'):
                if line.startswith('VERSION') and line[12:].strip() != "":
                    contig_id = line[12:].strip()
                line = lines.pop()
            # If no contig ids were filled after VERSION, we use what was found in LOCUS for the contig ID.
            # Should be unique in a dataset, but if there's an update
            # the contig ID might still be the same even though it should not(?)
        try:
            contig = organism.get(contig_id)
        except KeyError:
            with contig_counter.get_lock():
                contig = Contig(contig_counter.value, contig_id,
                                True if contig_id in circular_contigs or is_circ else False)
                contig_counter.value += 1
            organism.add(contig)
            contig.length = contig_len
        # start of the feature object.
        dbxref = set()
        gene_name = ""
        product = ""
        locus_tag = ""
        obj_type = ""
        protein_id = ""
        # if there is no /transl_table field, we assume it's 11.
        genetic_code = 11
        useful_info = False
        start = None
        stop = None
        strand = None
        line = lines.pop()
        while not line.startswith("ORIGIN"):
            curr_type = line[5:21].strip()
            if curr_type != "":
                if useful_info:
                    create_gene(organism, contig, gene_counter, rna_counter, locus_tag, dbxref, start, stop, strand,
                                obj_type, contig.number_of_genes, gene_name, product, genetic_code, protein_id)
                    if obj_type == "CDS":
                        gene_counter += 1
                    else:
                        rna_counter += 1
                useful_info = False
                obj_type = curr_type
                if obj_type in ['CDS', 'rRNA', 'tRNA']:
                    dbxref = set()
                    gene_name = ""
                    try:
                        if 'join' not in line[21:]:
                            useful_info = True
                            if line[21:].startswith('complement('):
                                strand = "-"
                                start, stop = line[32:].strip().replace(')', '').split("..")
                            else:
                                strand = "+"
                                start, stop = line[21:].strip().split('..')
                            if '>' in start or '<' in start or '>' in stop or '<' in stop:
                                if not pseudo:
                                    # pseudogene likely
                                    useful_info = False
                                else:
                                    start = start.replace('>', '').replace('<', '')
                                    stop = stop.replace('>', '').replace('<', '')
                            start, stop = map(int, [start, stop])
                    except ValueError:
                        pass
                        # don't know what to do with that, ignoring for now.
                        # there is a protein with a frameshift mecanism.
            elif useful_info:  # current info goes to current objtype, if it's useful.
                if line[21:].startswith("/db_xref"):
                    dbxref.add(line.split("=")[1].replace('"', '').strip())
                elif line[21:].startswith("/locus_tag"):
                    locus_tag = line.split("=")[1].replace('"', '').strip()
                elif line[21:].startswith("/protein_id"):
                    protein_id = line.split("=")[1].replace('"', '').strip()
                elif line[21:].startswith('/gene'):  # gene name
                    gene_name = line.split("=")[1].replace('"', '').strip()
                elif line[21:].startswith('/transl_table'):
                    genetic_code = int(line.split("=")[1].replace('"', '').strip())
                elif line[21:].startswith('/product'):  # need to loop as it can be more than one line long
                    product = line.split('=')[1].replace('"', '').strip()
                    if line.count('"') == 1:  # then the product line is on multiple lines
                        line = lines.pop()
                        product += line.strip().replace('"', '')
                        while line.count('"') != 1:
                            line = lines.pop()
                            product += line.strip().replace('"', '')
                # if it's a pseudogene, we're not keeping it, unless pseudo
                elif line[21:].startswith("/pseudo") and not pseudo:
                    useful_info = False
                # that's probably a 'stop' codon into selenocystein.
                elif line[21:].startswith("/transl_except") and not pseudo:
                    useful_info = False
            line = lines.pop()
            # end of contig
        if useful_info:  # saving the last element...
            create_gene(organism, contig, gene_counter, rna_counter, locus_tag, dbxref, start, stop, strand, obj_type,
                        contig.number_of_genes, gene_name, product, genetic_code, protein_id)
            if obj_type == "CDS":
                gene_counter += 1
            else:
                rna_counter += 1

        # now extract the gene sequences
        line = lines.pop()  # first sequence line.
        # if the seq was to be gotten, it would be here.
        sequence = ""
        while not line.startswith('//'):
            sequence += line[10:].replace(" ", "").strip().upper()
            line = lines.pop()

        if contig.length != len(sequence):
            raise ValueError("The contig length defined is different than the sequence length")
        # get each gene's sequence.
        for gene in contig.genes:
            gene.add_sequence(get_dna_sequence(sequence, gene))
    return organism, True


def read_org_gff(organism: str, gff_file_path: Path, circular_contigs: List[str],
                 pseudo: bool = False) -> Tuple[Organism, bool]:
    """
    Read annotation from GFF file

    :param organism: Organism name
    :param gff_file_path: Path corresponding to GFF file
    :param circular_contigs: List of circular contigs
    :param pseudo: Allow to read pseudogene

    :return: Organism object and if there are sequences associated or not
    """
    # TODO: This function would need some refactoring. 

    global ctg_counter

    (gff_seqname, _, gff_type, gff_start, gff_end, _, gff_strand, _, gff_attribute) = range(0, 9)

    # Missing values: source, score, frame. They are unused.
    def get_gff_attributes(gff_fields: list) -> dict:
        """Parses the gff attribute's line and outputs the attributes_get in a dict structure.

        :param gff_fields: A gff line stored as a list. Each element of the list is a column of the gff.

        :return: Attributes get
        """
        attributes_field = [f for f in gff_fields[gff_attribute].strip().split(';') if len(f) > 0]
        attributes_get = {}
        for att in attributes_field:
            try:
                (key, value) = att.strip().split('=')
                attributes_get[key.upper()] = value
            except ValueError:
                pass  # we assume that it is a strange, but useless field for our analysis
        return attributes_get

    def get_id_attribute(attributes_dict: dict) -> str:
        """
        Gets the ID of the element from which the provided attributes_get were extracted.
        Raises an error if no ID is found.

        :param attributes_dict: Attributes from one gff line

        :return: CDS identifier
        """
        element_id = attributes_dict.get("ID")
        if not element_id:
            raise Exception(f"Each CDS type of the gff files must own a unique ID attribute. "
                            f"Not the case for file: {gff_file_path} with ID {element_id}")
        return element_id

    contig = None  # initialize contig
    has_fasta = False
    fasta_string = ""
    org = Organism(organism)
    gene_counter = 0
    rna_counter = 0
    attr_prodigal = None

    with read_compressed_or_not(gff_file_path) as gff_file:
        for line in gff_file:
            if has_fasta:
                fasta_string += line
                continue

            elif line.startswith('##', 0, 2):
                if line.startswith('FASTA', 2, 7):
                    has_fasta = True
                elif line.startswith('sequence-region', 2, 17):
                    fields = [el.strip() for el in line.split()]
                    with contig_counter.get_lock():
                        contig = Contig(contig_counter.value, fields[1],
                                        True if fields[1] in circular_contigs else False)
                        contig_counter.value += 1
                    org.add(contig)
                    contig.length = int(fields[-1]) - int(fields[2]) + 1
                else:
                    continue

            elif line.startswith('#'):
                if line.startswith('Sequence Data', 2, 15):  # GFF from prodigal
                    fields_prodigal = [el.strip() for el in line.split(': ')[1].split(";")]
                    attr_prodigal = {field.split("=")[0]: field.split("=")[1] for field in fields_prodigal}
                else:  # comment lines to be ignores by parsers
                    continue

            elif line == "":  # empty lines are not expected, but they do not carry information, so we'll ignore them
                continue

            else:
                fields_gff = [el.strip() for el in line.split('\t')]
                attributes = get_gff_attributes(fields_gff)
                pseudogene = False

                if fields_gff[gff_type] == 'region':
                    if fields_gff[gff_seqname] in circular_contigs or ('IS_CIRCULAR' in attributes and
                                                                       attributes['IS_CIRCULAR']=="true"):
                        # WARNING: In case we have prodigal gff with is_circular attributes. 
                        # This would fail as contig is not defined. However is_circular should not be found in prodigal gff
                        logging.getLogger("PPanGGOLiN").debug(f"Contig {contig.name} is circular.")
                        contig.is_circular = True
                        assert contig.name == fields_gff[gff_seqname]

                elif fields_gff[gff_type] == 'CDS' or "RNA" in fields_gff[gff_type]:
                    gene_id = attributes.get("PROTEIN_ID")
                    # if there is a 'PROTEIN_ID' attribute, it's where the ncbi stores the actual gene ids, so we use that.

                    if gene_id is None:
                        # if it's not found, we get the one under the 'ID' field which must exist
                        # (otherwise not a gff3 compliant file)
                        gene_id = get_id_attribute(attributes)
                    
                    name = attributes.pop('NAME', attributes.pop('GENE', ""))
                    
                    if "PSEUDO" in attributes or "PSEUDOGENE" in attributes:
                        pseudogene = True
                    
                    product = attributes.pop('PRODUCT', "")
                    genetic_code = int(attributes.pop("TRANSL_TABLE", 11))
                    
                    if contig is None or contig.name != fields_gff[gff_seqname]:
                        # get the current contig
                        try:
                            contig = org.get(fields_gff[gff_seqname])
                        except KeyError:
                            with contig_counter.get_lock():
                                contig = Contig(contig_counter.value, fields_gff[gff_seqname],
                                                True if fields_gff[gff_seqname] in circular_contigs else False)
                                contig_counter.value += 1
                            org.add(contig)
                            if attr_prodigal is not None:
                                contig.length = int(attr_prodigal["seqlen"])
                    if fields_gff[gff_type] == "CDS" and (not pseudogene or (pseudogene and pseudo)):
                        gene = Gene(org.name + "_CDS_" + str(gene_counter).zfill(4))
                        # here contig is filled in order, so position is the number of genes already stored in the contig.
                        gene.fill_annotations(start=int(fields_gff[gff_start]), stop=int(fields_gff[gff_end]),
                                              strand=fields_gff[gff_strand], gene_type=fields_gff[gff_type], name=name,
                                              position=contig.number_of_genes, product=product, local_identifier=gene_id,
                                              genetic_code=genetic_code)
                        gene.fill_parents(org, contig)
                        gene_counter += 1
                        contig.add(gene)

                    elif "RNA" in fields_gff[gff_type]:
                        rna_type = fields_gff[gff_type]
                        rna = RNA(org.name + f"_{rna_type}_" + str(rna_counter).zfill(4))
                        rna.fill_annotations(start=int(fields_gff[gff_start]), stop=int(fields_gff[gff_end]),
                                             strand=fields_gff[gff_strand], gene_type=fields_gff[gff_type], name=name,
                                             product=product, local_identifier=gene_id)
                        rna.fill_parents(org, contig)
                        rna_counter += 1
                        contig.add_rna(rna)

    # GET THE FASTA SEQUENCES OF THE GENES
    if has_fasta and fasta_string != "":
        contig_sequences = read_fasta(org, fasta_string.split('\n'))  # _ is total contig length
        for contig in org.contigs:
            if contig.length != len(contig_sequences[contig.name]):
                raise ValueError("The contig lenght defined is different than the sequence length")

            for gene in contig.genes:
                gene.add_sequence(get_dna_sequence(contig_sequences[contig.name], gene))
            for rna in contig.RNAs:
                rna.add_sequence(get_dna_sequence(contig_sequences[contig.name], rna))
    return org, has_fasta


def read_anno_file(organism_name: str, filename: Path, circular_contigs: list,
                   pseudo: bool = False) -> Tuple[Organism, bool]:
    """
    Read a GBFF file for one organism

    :param organism_name: Name of the organism
    :param filename: Path to the corresponding file
    :param circular_contigs: list of sequence in contig
    :param pseudo: allow to read pseudogene

    :return: Annotated organism for pangenome and true for sequence in file
    """
    global ctg_counter
    filetype = detect_filetype(filename)
    if filetype == "gff":
        try:
            org, has_fasta = read_org_gff(organism_name, filename, circular_contigs, pseudo)
        except Exception as err:
            raise Exception(f"Reading the gff3 file '{filename}' raised an error. {err}")
        else:
            return org, has_fasta
    elif filetype == "gbff":
        try:
            org, has_fasta = read_org_gbff(organism_name, filename, circular_contigs, pseudo)
        except Exception as err:
            raise Exception(f"Reading the gbff file '{filename}' raised an error. {err}")
        else:
            return org, has_fasta
    elif filetype == "fasta":
        raise ValueError(f"Invalid file type provided for parameter '--anno'. "
                         f"The file '{filename}' looks like a fasta file. "
                         "Please use a .gff or .gbff file. You may be able to use --fasta instead of --anno.")

    else:
        raise ValueError(f"Invalid file type provided for parameter '--anno'. "
                         f"The file '{filename}' appears to be of type '{filetype}'. Please use .gff or .gbff files.")



def chose_gene_identifiers(pangenome: Pangenome) -> bool:
    """
    Parses the pangenome genes to decide whether to use local_identifiers or ppanggolin generated gene identifiers.
    If the local identifiers are unique within the pangenome they are picked, otherwise ppanggolin ones are used.

    :param pangenome: input pangenome

    :return: Boolean stating True if local identifiers are used, and False otherwise
    """

    if local_identifiers_are_unique(pangenome.genes):

        for gene in pangenome.genes:
            gene.ID = gene.local_identifier  # Erase ppanggolin generated gene ids and replace with local identifiers
            gene.local_identifier = ""  # this is now useless, setting it to default value
        pangenome._mk_gene_getter()  # re-build the gene getter
        return True

    else:
        return False


def local_identifiers_are_unique(genes: Iterable[Gene]) -> bool:
    """
    Check if local_identifiers of genes are uniq in order to decide if they should be used as gene id.

    :param genes: Iterable of gene objects

    :return: Boolean stating True if local identifiers are uniq, and False otherwise
    """
    gene_id_2_local = {}
    local_to_gene_id = {}
    for gene in genes:
        gene_id_2_local[gene.ID] = gene.local_identifier
        local_to_gene_id[gene.local_identifier] = gene.ID
        if len(local_to_gene_id) != len(gene_id_2_local):
            # then, there are non unique local identifiers
            return False
    # if we reach this line, local identifiers are unique within the pangenome
    return True


def read_annotations(pangenome: Pangenome, organisms_file: Path, cpu: int = 1, pseudo: bool = False,
                     disable_bar: bool = False):
    """
    Read the annotation from GBFF file

    :param pangenome: pangenome object
    :param organisms_file: List of GBFF files for each organism
    :param cpu: number of CPU cores to use
    :param pseudo: allow to read pseudogène
    :param disable_bar: Disable the progress bar
    """

    logging.getLogger("PPanGGOLiN").info(f"Reading {organisms_file.name} the list of genome files ...")

    pangenome.status["geneSequences"] = "Computed"
    # we assume there are gene sequences in the annotation files,
    # unless a gff file without fasta is met (which is the only case where sequences can be absent)
    args = []
    for line in read_compressed_or_not(organisms_file):
        elements = [el.strip() for el in line.split("\t")]
        org_path = Path(elements[1])
        if not org_path.exists():  # Check tsv sanity test if it's not one it's the other
            org_path = organisms_file.parent.joinpath(org_path)
        args.append((elements[0], org_path, elements[2:], pseudo))

    with ProcessPoolExecutor(mp_context=get_context('fork'), max_workers=cpu,
                             initializer=init_contig_counter, initargs=(contig_counter,)) as executor:
        with tqdm(total=len(args), unit="file", disable=disable_bar) as progress:
            futures = []

            for fn_args in args:
                future = executor.submit(read_anno_file, *fn_args)
                future.add_done_callback(lambda p: progress.update())
                futures.append(future)

            for future in futures:
                org, flag = future.result()
                pangenome.add_organism(org)
                if not flag:
                    pangenome.status["geneSequences"] = "No"

    # decide whether we use local ids or ppanggolin ids.
    used_local_identifiers = chose_gene_identifiers(pangenome)
    if used_local_identifiers:
        logging.getLogger("PPanGGOLiN").info("gene identifiers used in the provided annotation files were unique, "
                                             "PPanGGOLiN will use them.")
    else:
        logging.getLogger("PPanGGOLiN").info("gene identifiers used in the provided annotation files were not unique, "
                                             "PPanGGOLiN will use self-generated identifiers.")

    pangenome.status["genomesAnnotated"] = "Computed"
    pangenome.parameters["annotate"] = {}
    pangenome.parameters["annotate"]["# used_local_identifiers"] = used_local_identifiers
    pangenome.parameters["annotate"]["use_pseudo"] = pseudo
    pangenome.parameters["annotate"]["# read_annotations_from_file"] = True


def get_gene_sequences_from_fastas(pangenome: Pangenome, fasta_files: Path, disable_bar: bool = False):
    """
    Get gene sequences from fastas

    :param pangenome: Input pangenome
    :param fasta_files: list of fasta file
    :param disable_bar: Flag to disable progress bar
    """
    fasta_dict = {}
    for line in read_compressed_or_not(fasta_files):
        elements = [el.strip() for el in line.split("\t")]
        if len(elements) <= 1:
            logging.getLogger("PPanGGOLiN").error("No tabulation separator found in genome file")
            exit(1)
        try:
            org = pangenome.get_organism(elements[0])
        except KeyError:
            raise KeyError(f"One of the genome in your '{fasta_files}' was not found in the pan."
                           f" This might mean that the genome names between your annotation file and "
                           f"your fasta file are different.")
        with read_compressed_or_not(Path(elements[1])) as currFastaFile:
            fasta_dict[org] = read_fasta(org, currFastaFile)
    if set(pangenome.organisms) > set(fasta_dict.keys()):
        missing = pangenome.number_of_organisms - len(set(pangenome.organisms) & set(fasta_dict.keys()))
        raise KeyError(f"Not all of your pangenome genomes are present within the provided fasta file. "
                       f"{missing} are missing (out of {pangenome.number_of_organisms}).")

    with tqdm(total=pangenome.number_of_genes, unit="gene", disable=disable_bar,
              desc="Add sequences to genes") as bar:
        for org in pangenome.organisms:
            for contig in org.contigs:
                try:
                    for gene in contig.genes:
                        gene.add_sequence(get_dna_sequence(fasta_dict[org][contig.name], gene))
                        bar.update()
                    # for rna in contig.RNAs:
                    #     rna.add_sequence(get_dna_sequence(fasta_dict[org][contig.name], rna))
                except KeyError:
                    msg = f"Fasta file for genome {org.name} did not have the contig {contig.name} " \
                          f"that was read from the annotation file. "
                    msg += f"The provided contigs in the fasta were : " \
                           f"{', '.join([contig for contig in fasta_dict[org].keys()])}."
                    raise KeyError(msg)
    pangenome.status["geneSequences"] = "Computed"


def annotate_pangenome(pangenome: Pangenome, fasta_list: Path, tmpdir: str, cpu: int = 1, translation_table: int = 11,
                       kingdom: str = "bacteria", norna: bool = False, allow_overlap: bool = False,
                       procedure: str = None, disable_bar: bool = False):
    """
    Main function to annotate a pangenome

    :param pangenome: Pangenome with gene families to align with the given input sequences
    :param fasta_list: List of fasta file containing sequences that will be base of pangenome
    :param tmpdir: Path to temporary directory
    :param cpu: number of CPU cores to use
    :param translation_table: Translation table (genetic code) to use.
    :param kingdom: Kingdom to which the prokaryota belongs to, to know which models to use for rRNA annotation.
    :param norna: Use to avoid annotating RNA features.
    :param allow_overlap: Use to not remove genes overlapping with RNA features
    :param procedure: prodigal procedure used
    :param disable_bar: Disable the progress bar
    """

    logging.getLogger("PPanGGOLiN").info(f"Reading {fasta_list} the list of genome files")

    arguments = []  # Argument given to annotate organism in same order than prototype
    for line in read_compressed_or_not(fasta_list):

        elements = [el.strip() for el in line.split("\t")]
        org_path = Path(elements[1])

        if not org_path.exists():  # Check tsv sanity test if it's not one it's the other
            org_path = fasta_list.parent.joinpath(org_path)

        arguments.append((elements[0], org_path, elements[2:], tmpdir, translation_table,
                          norna, kingdom, allow_overlap, procedure))

    if len(arguments) == 0:
        raise Exception("There are no genomes in the provided file")

    logging.getLogger("PPanGGOLiN").info(f"Annotating {len(arguments)} genomes using {cpu} cpus...")
    with ProcessPoolExecutor(mp_context=get_context('fork'), max_workers=cpu,
                             initializer=init_contig_counter, initargs=(contig_counter,)) as executor:
        with tqdm(total=len(arguments), unit="file", disable=disable_bar) as progress:
            futures = []

            for fn_args in arguments:
                future = executor.submit(annotate_organism, *fn_args)
                future.add_done_callback(lambda p: progress.update())
                futures.append(future)

            for future in futures:
                pangenome.add_organism(future.result())

    logging.getLogger("PPanGGOLiN").info("Done annotating genomes")
    pangenome.status["genomesAnnotated"] = "Computed"  # the pangenome is now annotated.
    pangenome.status["geneSequences"] = "Computed"  # the gene objects have their respective gene sequences.
    pangenome.parameters["annotate"] = {}
    pangenome.parameters["annotate"]["norna"] = norna
    pangenome.parameters["annotate"]["kingdom"] = kingdom
    pangenome.parameters["annotate"]["translation_table"] = translation_table
    pangenome.parameters["annotate"]["prodigal_procedure"] = None if procedure is None else procedure
    pangenome.parameters["annotate"]["allow_overlap"] = allow_overlap
    pangenome.parameters["annotate"]["# read_annotations_from_file"] = False


def launch(args: argparse.Namespace):
    """
    Command launcher

    :param args: All arguments provide by user
    """
    check_annotate_args(args)
    filename = mk_file_name(args.basename, args.output, args.force)
    pangenome = Pangenome()
    if args.fasta is not None and args.anno is None:
        annotate_pangenome(pangenome, args.fasta, tmpdir=args.tmpdir, cpu=args.cpu, procedure=args.prodigal_procedure,
                           translation_table=args.translation_table, kingdom=args.kingdom, norna=args.norna,
                           allow_overlap=args.allow_overlap, disable_bar=args.disable_prog_bar)
    elif args.anno is not None:
        # TODO add warning for option not compatible with read_annotations
        read_annotations(pangenome, args.anno, cpu=args.cpu, pseudo=args.use_pseudo, disable_bar=args.disable_prog_bar)
        if pangenome.status["geneSequences"] == "No":
            if args.fasta:
                logging.getLogger("PPanGGOLiN").info(f"Get sequences from FASTA file: {args.fasta}")
                get_gene_sequences_from_fastas(pangenome, args.fasta)
            else:
                logging.getLogger("PPanGGOLiN").warning("You provided gff files without sequences, "
                                                        "and you did not provide fasta sequences. "
                                                        "Thus it was not possible to get the gene sequences.")
                logging.getLogger("PPanGGOLiN").warning("You will be able to proceed with your analysis "
                                                        "ONLY if you provide the clustering results in the next step.")
        else:
            if args.fasta:
                logging.getLogger("PPanGGOLiN").warning("You provided fasta sequences "
                                                        "but your gff files were already with sequences."
                                                        "PPanGGOLiN will use sequences in GFF and not from your fasta.")
    write_pangenome(pangenome, filename, args.force, disable_bar=args.disable_prog_bar)


def subparser(sub_parser: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """
    Subparser to launch PPanGGOLiN in Command line

    :param sub_parser : sub_parser for align command

    :return : parser arguments for align command
    """
    parser = sub_parser.add_parser("annotate", formatter_class=argparse.RawTextHelpFormatter)
    parser_annot(parser)
    return parser


def parser_annot(parser: argparse.ArgumentParser):
    """
    Parser for specific argument of annotate command

    :param parser: parser for annotate argument
    """
    date = time.strftime("_DATE%Y-%m-%d_HOUR%H.%M.%S", time.localtime())
    required = parser.add_argument_group(title="Required arguments",
                                         description="One of the following arguments is required :")
    required.add_argument('--fasta', required=False, type=Path,
                          help="A tab-separated file listing the genome names, and the fasta filepath of its genomic "
                               "sequence(s) (the fastas can be compressed with gzip). One line per genome.")
    required.add_argument('--anno', required=False, type=Path,
                          help="A tab-separated file listing the genome names, and the gff/gbff filepath of its "
                               "annotations (the files can be compressed with gzip). One line per genome. "
                               "If this is provided, those annotations will be used.")

    optional = parser.add_argument_group(title="Optional arguments")
    optional.add_argument('-o', '--output', required=False, type=Path,
                          default=Path(f'ppanggolin_output{date}_PID{str(os.getpid())}'),
                          help="Output directory")
    optional.add_argument('--allow_overlap', required=False, action='store_true', default=False,
                          help="Use to not remove genes overlapping with RNA features.")
    optional.add_argument("--norna", required=False, action="store_true", default=False,
                          help="Use to avoid annotating RNA features.")
    optional.add_argument("--kingdom", required=False, type=str.lower, default="bacteria",
                          choices=["bacteria", "archaea"],
                          help="Kingdom to which the prokaryota belongs to, "
                               "to know which models to use for rRNA annotation.")
    optional.add_argument("--translation_table", required=False, type=int, default=11,
                          help="Translation table (genetic code) to use.")
    optional.add_argument("--basename", required=False, default="pangenome", help="basename for the output file")
    optional.add_argument("--use_pseudo", required=False, action="store_true",
                          help="In the context of provided annotation, use this option to read pseudogenes. "
                               "(Default behavior is to ignore them)")
    optional.add_argument("-p", "--prodigal_procedure", required=False, type=str.lower, choices=["single", "meta"],
                          default=None, help="Allow to force the prodigal procedure. "
                                             "If nothing given, PPanGGOLiN will decide in function of contig length")
    optional.add_argument("-c", "--cpu", required=False, default=1, type=int, help="Number of available cpus")
    optional.add_argument("--tmpdir", required=False, type=str, default=Path(tempfile.gettempdir()),
                          help="directory for storing temporary files")


if __name__ == '__main__':
    """To test local change and allow using debugger"""
    from ppanggolin.utils import set_verbosity_level, add_common_arguments

    main_parser = argparse.ArgumentParser(
        description="Depicting microbial species diversity via a Partitioned PanGenome Graph Of Linked Neighbors",
        formatter_class=argparse.RawTextHelpFormatter)

    parser_annot(main_parser)
    add_common_arguments(main_parser)
    set_verbosity_level(main_parser.parse_args())
    launch(main_parser.parse_args())
