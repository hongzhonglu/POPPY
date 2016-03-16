#!/usr/bin/env python3

# Import modules
import networkx as nx
import MineClient3 as mc
import re
import sys
import argparse
import pickle
import time
import queue
import threading
from requests import get as rget
from rdkit import Chem
from copy import deepcopy
from itertools import repeat

# Define functions
def sWrite(string):
    sys.stdout.write(string)
    sys.stdout.flush()


def sError(string):
    sys.stderr.write(string)
    sys.stderr.flush()


def GetKeggText(kegg_id, krest="http://rest.kegg.jp"):
    """
    Downloads the raw text entry for the provided KEGG compound or reaction ID,
    via the KEGG rest API @ http://rest.kegg.jp/
    """

    # Check ID and construct query
    if re.fullmatch("^R[0-9]{5}$", kegg_id):
        # KEGG reactions
        krest = "/".join([krest,"get","rn:"+kegg_id])
    elif re.fullmatch("^C[0-9]{5}$", kegg_id):
        # KEGG compounds
        krest = "/".join([krest,"get","cpd:"+kegg_id])
    else:
        # Invalid ID
        sError("Warning: '%s' is not a valid KEGG reaction or compound ID.\n" % str(kegg_id))
        return None

    n = 0

    while True:
        r = rget(krest)
        if r.status_code == 200:
            return r.text
        else:
            # Server not returning a result, try again
            n += 1
            if n >= 5:
                sError("Warning: Unable to download KEGG data for '%s'.\n" % str(kegg_id))
                return None
            time.sleep(2)

def test_GetKeggText(capsys):
    kegg1 = "R01393"
    kegg2 = "C00001"
    kegg3 = "C99999"
    kegg4 = "X99999"
    assert GetKeggText(kegg1) == rget("http://rest.kegg.jp/get/rn:R01393").text
    assert GetKeggText(kegg2) == rget("http://rest.kegg.jp/get/cpd:C00001").text
    assert GetKeggText(kegg3) == None
    assert GetKeggText(kegg4) == None
    out, err = capsys.readouterr()
    assert err == "Warning: Unable to download KEGG data for 'C99999'.\nWarning: 'X99999' is not a valid KEGG reaction or compound ID.\n"


def KeggRestDict(kegg_text):
    """
    Parses a KEGG rest text record into a dictionary. Accepts a single record,
    as it stops after the first '///'.
    """

    kegg_dict = {}

    for line in kegg_text.split("\n"):
        if line == "///":
            # The first entry ends here
            break
        if not line.startswith(" "):
            line = line.split()
            key = line[0]
            line = line[1:]
            try:
                kegg_dict[key].extend(line)
            except KeyError:
                kegg_dict[key] = line
        else:
            try:
                kegg_dict[key].extend(line.split())
            except NameError:
                sError("Warning: KEGG text line '%s' has no key." % line)

    return kegg_dict

def test_KeggRestDict():
    assert KeggRestDict(GetKeggText("R05735"))['ENZYME'] == ['6.4.1.6']
    assert KeggRestDict(GetKeggText("C18020"))['FORMULA'] == ['C10H18O2']
    assert set(KeggRestDict(GetKeggText("C18020")).keys()) == set([
        "ENTRY","NAME","FORMULA",
        "EXACT_MASS","MOL_WEIGHT","REACTION",
        "ENZYME","DBLINKS","ATOM","BOND"
        ])
    assert KeggRestDict(GetKeggText("C00999"))['NAME'] == ["Ferrocytochrome","b5"]
    assert len(KeggRestDict(GetKeggText("C00999"))['ENZYME']) == 21
    assert KeggRestDict(GetKeggText("R06519"))['EQUATION'][9] == "<=>"


def FormatKeggReaction(kegg_text):
    """Formats a reaction KEGG rest text record in the MINE database format."""

    # Parse the text into a dictionary
    kegg_dict = KeggRestDict(kegg_text)

    # Check that the needed keys are there
    # ENZYME is missing for known non-enzymatic reactions

    # Ensure that there is an ID
    if "ENTRY" not in kegg_dict.keys():
        sError("\nWarning: The following KEGG reaction record lacks an ENTRY key: '%s'\n" % str(kegg_dict))
        return None

    # Get the ID
    _id = kegg_dict['ENTRY'][0]

    # Check that the record deals with a compound
    if not re.fullmatch("^R[0-9]{5}$", _id):
        sError("\nWarning: '%s' is not a valid KEGG compound ID.\n" % str(_id))
        return None

    # Ensure that there is an EQUATION
    if "EQUATION" not in kegg_dict.keys():
        sError("\nWarning: The following KEGG reaction record lacks an EQUATION key: '%s'\n" % str(kegg_dict))
        return None

    # Get the Operators (enzyme ECs)
    try:
        Operators = kegg_dict['ENZYME']
    except KeyError:
        Operators = ['NA']

    # Re-format the equation
    reactants = True
    n = 1

    Reactants = []
    Products = []

    for segment in kegg_dict['EQUATION']:
        if segment == "<=>":
            reactants = False
            n = 1
            continue
        if segment == "+":
            n = 1
            continue
        if re.fullmatch("^[0-9]+$", segment):
            n = int(segment)
            continue
        # If we make it here, we have a compound ID
        if reactants:
            Reactants.append([n,segment])
        else:
            Products.append([n,segment])

    # Parse the reactant pairs
    RPair = {}
    if 'RPAIR' in kegg_dict.keys():
        p_list = []
        for segment in kegg_dict['RPAIR']:
            if segment.startswith('['):
                continue
            p_list.append(segment)
            if len(p_list) == 3:
                RPair[p_list[0]] = (p_list[1], p_list[2])
                p_list = []
        # The p_list should be empty at the end of iteration
        if len(p_list):
            sError("Warning: Unexpected format of RPair list '%s'." % str(kegg_dict['RPAIR']))

    return {"_id":_id,"Operators":Operators,"Reactants":Reactants,"Products":Products,"RPair":RPair}

def test_FormatKeggReaction():
    _id = "R01393"
    Operators = ["4.1.1.40"]
    Reactants = [[1,"C00168"]]
    Products = [[1,"C00266"],[1,"C00011"]]
    RPair = {"RP01475":("C00168_C00266","main"),"RP06553":("C00011_C00168","leave")}
    rxn1 = {"_id":_id,"Operators":Operators,"Reactants":Reactants,"Products":Products,"RPair":RPair}

    _id = "R05735"
    Operators = ["6.4.1.6"]
    Reactants = [[1,"C00207"],[1,"C00011"],[1,"C00002"],[2,"C00001"]]
    Products = [[1,"C00164"],[1,"C00020"],[2,"C00009"]]
    RPair = {
        "RP00010":("C00002_C00009","ligase"),
        "RP00274":("C00164_C00207","main"),
        "RP05676":("C00001_C00009","leave"),
        "RP05804":("C00011_C00164","leave"),
        "RP12346":("C00002_C00020","ligase"),
        "RP12391":("C00002_C00009","ligase")
    }
    rxn2 = {"_id":_id,"Operators":Operators,"Reactants":Reactants,"Products":Products,"RPair":RPair}

    _id = "R06519"
    Operators = ["1.14.19.17"]
    Reactants = [[1,"C12126"],[2,"C00999"],[1,"C00007"],[2,"C00080"]]
    Products = [[1,"C00195"],[2,"C00996"],[2,"C00001"]]
    RPair = {"RP00013":("C00001_C00007","cofac"),"RP05667":("C00195_C12126","main")}
    rxn3 = {"_id":_id,"Operators":Operators,"Reactants":Reactants,"Products":Products,"RPair":RPair}

    _id = "R00178"
    Operators = ["4.1.1.50"]
    Reactants = [[1,"C00019"],[1,"C00080"]]
    Products = [[1,"C01137"],[1,"C00011"]]
    RPair = {"RP03935":("C00019_C01137","main"),"RP08122":("C00011_C00019","leave")}
    rxn4 = {"_id":_id,"Operators":Operators,"Reactants":Reactants,"Products":Products,"RPair":RPair}

    assert FormatKeggReaction(GetKeggText("R01393")) == rxn1
    assert FormatKeggReaction(GetKeggText("R05735")) == rxn2
    assert FormatKeggReaction(GetKeggText("R06519")) == rxn3
    assert FormatKeggReaction(GetKeggText("R00178")) == rxn4


def GetKeggMolSmiles(kegg_id, krest="http://rest.kegg.jp"):
    """Downloads a KEGG compound molecule object and converts it to SMILES."""

    if not re.fullmatch("^C[0-9]{5}$", kegg_id):
        sError("\nWarning: '%s' is not a valid KEGG compound ID.\n" % str(kegg_id))
        return None

    # Set up the query
    krest = "/".join([krest,"get","cpd:"+kegg_id,"mol"])

    # Contact server (several times if necessary)
    n = 0
    while True:
        r = rget(krest)
        if r.status_code == 200:
            mol = Chem.MolFromMolBlock(r.text)
            if mol == None:
                sError("\nWarning: KEGG ID '%s' does not yield a correct molecule object. SMILES not produced.\n" % str(kegg_id))
                return None
            else:
                return Chem.MolToSmiles(mol)
        else:
            # Server not returning a result, try again
            n += 1
            if n >= 5:
                sError("\nWarning: Unable to download molecule data for '%s'.\n" % str(kegg_id))
                return None
            time.sleep(2)

def test_GetKeggMolSmiles(capsys):
    assert GetKeggMolSmiles("C06099") == "C=C(C)C1CC=C(C)CC1"
    assert GetKeggMolSmiles("C09908") == "Cc1ccc(C(C)C)c(O)c1"
    assert GetKeggMolSmiles("C06142") == "CCCCO"
    assert GetKeggMolSmiles("C01412") == "CCCC=O"
    assert GetKeggMolSmiles("XYZ") == None
    assert GetKeggMolSmiles("C00999") == None
    assert GetKeggMolSmiles("C99999") == None

    out, err = capsys.readouterr()
    assert err == "".join([
        "\nWarning: 'XYZ' is not a valid KEGG compound ID.\n",
        "\nWarning: Unable to download molecule data for 'C00999'.\n",
        "\nWarning: Unable to download molecule data for 'C99999'.\n"
    ])

def FormatKeggCompound(kegg_text):
    """Formats a compound KEGG rest text record in the MINE database format."""
    kegg_dict = KeggRestDict(kegg_text)

    compound = {}

    # Ensure that there is an ID
    if "ENTRY" not in kegg_dict.keys():
        sError("\nWarning: The following KEGG compound record lacks an ENTRY key: '%s'\n" % str(kegg_dict))
        return None

    # Add ID
    compound['_id'] = kegg_dict['ENTRY'][0]

    # Check that the record deals with a compound
    if not re.fullmatch("^C[0-9]{5}$", compound['_id']):
        sError("\nWarning: '%s' is not a valid KEGG compound ID.\n" % str(compound['_id']))
        return None

    # Add DB_links
    compound['DB_links'] = {'KEGG':[compound['_id']]}

    # Add SMILES if possible
    smiles = GetKeggMolSmiles(compound['_id'])
    if smiles:
        compound['SMILES'] = smiles

    # Add Names if possible
    if 'NAME' in kegg_dict.keys():
        names = []
        name = []
        for segment in kegg_dict['NAME']:
            if segment.endswith(";"):
                name.append(segment.rstrip(";"))
                names.append(" ".join(name))
                name = []
            else:
                name.append(segment)
        if len(name):
            # Catch trailing name
            names.append(" ".join(name))
        compound['Names'] = names

    # Add Formula if possible
    if 'FORMULA' in kegg_dict.keys():
        compound['Formula'] = kegg_dict['FORMULA'][0]

    # Add reactions if possible
    if 'REACTION' in kegg_dict.keys():
        compound['Reactions'] = kegg_dict['REACTION']

    return compound

def test_FormatKeggCompound():
    comps = [
        {"_id" : "C06142",
        "SMILES" : "CCCCO",
        "Reactions" : ['R03544','R03545'],
        "Names" : ['1-Butanol','n-Butanol'],
        "DB_links" : {'KEGG':['C06142']},
        "Formula" : "C4H10O"},
        {"_id" : "C00999",
        "Reactions" : KeggRestDict(GetKeggText("C00999"))['REACTION'],
        "Names" : ["Ferrocytochrome b5"],
        "DB_links" : {'KEGG':['C00999']}},
        {"_id" : "C00006",
        "SMILES" : 'NC(=O)c1ccc[n+](C2OC(COP(=O)(O)OP(=O)(O)OCC3OC(n4cnc5c(N)ncnc54)C(OP(=O)(O)O)C3O)C(O)C2O)c1',
        "Reactions" : KeggRestDict(GetKeggText("C00006"))['REACTION'],
        "Names" : [
            "NADP+", "NADP", "Nicotinamide adenine dinucleotide phosphate",
            "beta-Nicotinamide adenine dinucleotide phosphate", "TPN",
            "Triphosphopyridine nucleotide", "beta-NADP+"],
        "DB_links" : {'KEGG':['C00006']},
        "Formula" : "C21H29N7O17P3"}
    ]

    for comp in comps:
        assert comp == FormatKeggCompound(GetKeggText(comp['_id']))


def AllowReactionListing(kegg_comp, kegg_rxn):
    # Is the compound inorganic or CO2?
    if not LimitCarbon(kegg_comp, 0) or kegg_comp['_id'] == "C00011":
        return False
    # Is the compound CoA or ACP?
    if kegg_comp['_id'] in {"C00010", "C00229"}:
        return False
    # Is the compound involved in a reaction as a cofactor or as part of RP00003?
    if "RPair" in kegg_rxn.keys():
        for rp in kegg_rxn['RPair'].items():
            if kegg_comp['_id'] in rp[1][0] and rp[1][1] == "cofac":
                return False
            if kegg_comp['_id'] in rp[1][0] and rp[0] == "RP00003":
                return False
    # If the compound passed all the tests, the reaction is free to be listed
    return True

def test_AllowReactionListing():
    # C1 is not a cofactor
    cpd = {"_id":"C1", "Reactions":['R1','R2'], "Formula":"C10"}
    rxn = {"_id":"R1", "RPair":{"RP1":("C1_C2","main"),"RP2":("C3_C4","cofac")}}
    assert AllowReactionListing(cpd, rxn)

    # C1 is a cofactor
    rxn = {"_id":"R2", "RPair":{"RP3":("C1_C5","cofac"),"RP4":("C6_C7","main")}}
    assert not AllowReactionListing(cpd, rxn)

    # C1 is not in an RPair
    rxn = {"_id":"R1", "RPair":{"RP1":("C5_C2","main"),"RP2":("C3_C4","cofac")}}
    assert AllowReactionListing(cpd, rxn)

    # ATP/ADP reaction pair should not be listed (RP00003)
    cpd = {"_id":"C00002", "Reactions":['R1','R2']}
    rxn = {"_id":"R1", "RPair":{"RP00003":("C00002_C00008","ligase")}}
    assert not AllowReactionListing(cpd, rxn)

    # ATP might be involved in other reactions
    cpd = FormatKeggCompound(GetKeggText("C00002"))
    rxn = FormatKeggReaction(GetKeggText("R00085")) # ATP -> AMP
    assert AllowReactionListing(cpd, rxn)

    # CoA should not be listed
    cpd = FormatKeggCompound(GetKeggText("C00010"))
    rxn = FormatKeggReaction(GetKeggText(cpd['Reactions'][201]))
    assert not AllowReactionListing(cpd, rxn)

    # ACP should not be listed
    cpd = FormatKeggCompound(GetKeggText("C00229"))
    rxn = FormatKeggReaction(GetKeggText(cpd['Reactions'][15]))
    assert not AllowReactionListing(cpd, rxn)

    # Water is often a cofactor and should not be listed
    cpd = FormatKeggCompound(GetKeggText("C00001"))
    rxn = FormatKeggReaction(GetKeggText(cpd['Reactions'][45]))
    assert not AllowReactionListing(cpd, rxn)

    # Inorganic compounds need to be disallowed
    cpd = FormatKeggCompound(GetKeggText("C00009"))
    rxn = FormatKeggReaction(GetKeggText(cpd['Reactions'][167]))
    assert not AllowReactionListing(cpd, rxn)

    # ...as well as CO2
    cpd = FormatKeggCompound(GetKeggText("C00011"))
    rxn = FormatKeggReaction(GetKeggText(cpd['Reactions'][89]))
    assert not AllowReactionListing(cpd, rxn)

    # Keep single-carbon reduced compounds though
    cpd = FormatKeggCompound(GetKeggText("C00132")) # Methanol
    rxn = FormatKeggReaction(GetKeggText(cpd['Reactions'][23]))
    assert AllowReactionListing(cpd, rxn)



def SortKeggReactions(kegg_comp_dict, kegg_rxn_dict, verbose=False):
    """
    Re-organizes reactions of a KEGG compound into 'Reactant_in' and
    'Product_of' categories based on the information contained in the reactions
    dictionary.
    """
    # Go through all compounds
    for kegg_comp_id in kegg_comp_dict.keys():
        kegg_comp = kegg_comp_dict[kegg_comp_id]
        # Go through its reactions
        if "Reactions" in kegg_comp.keys():
            for rxn_id in kegg_comp["Reactions"]:
                if rxn_id in kegg_rxn_dict.keys():
                    rxn = kegg_rxn_dict[rxn_id]
                else:
                    if verbose:
                        sError("Warning: KEGG compound '%s' lists missing reaction '%s'.\n" % (kegg_comp_id, rxn_id))
                    continue
                # Check if a reaction listing is allowed
                if not AllowReactionListing(kegg_comp, rxn):
                    continue
                # Add to Reactant_in list
                if "Reactants" in rxn.keys():
                    if kegg_comp_id in [x[1] for x in rxn["Reactants"]]:
                        try:
                            kegg_comp_dict[kegg_comp_id]['Reactant_in'].append(rxn["_id"])
                        except KeyError:
                            kegg_comp_dict[kegg_comp_id]['Reactant_in'] = [rxn["_id"]]
                # Add to Product_of list
                if "Products" in rxn.keys():
                    if kegg_comp_id in [x[1] for x in rxn["Products"]]:
                        try:
                            kegg_comp_dict[kegg_comp_id]['Product_of'].append(rxn["_id"])
                        except KeyError:
                            kegg_comp_dict[kegg_comp_id]['Product_of'] = [rxn["_id"]]



def test_SortKeggReactions():
    # C1 is cofactor in one reaction, not in another
    # C2 is inorganic
    # C3 is a reactant in one reaction, product in another
    # C4 is a product of C3, and lists a reaction that doesn't exist
    # C5 lists a reaction in which it is not listed
    # C6 does not list reactions
    kegg_comp_dict = {
        "C1":{"_id":"C1","Reactions":["R1","R2"],"Formula":"C10H18O2"},
        "C2":{"_id":"C2","Reactions":["R3","R4"],"Formula":"XeF4"},
        "C3":{"_id":"C3","Reactions":["R5","R6"],"Formula":"C10H12O3"},
        "C4":{"_id":"C4","Reactions":["R5","RX"],"Formula":"C2H5O"},
        "C5":{"_id":"C5","Reactions":["R7"],"Formula":"CH3O"},
        "C6":{"_id":"C6","Formula":"C12"}
    }
    kegg_rxn_dict = {
        "R1":{"_id":"R1","Reactants":[[1,"C1"]],"Products":[[1,"X1"]],"RPair":{"RP1":("C1_X1","main")}},
        "R2":{"_id":"R2","Reactants":[[1,"C1"],[1,"C100"]],"Products":[[1,"C101"],[2,"C10"]],"RPair":{"RP2":("C100_C101","main"),"RP3":("C1_C10","cofac")}},
        "R3":{"_id":"R3","Reactants":[[1,"C2"]],"Products":[[1,"X2"]],"RPair":{"RP4":("C2_X2","main")}},
        "R4":{"_id":"R3","Reactants":[[1,"Z2"]],"Products":[[1,"C2"]],"RPair":{"RP5":("Z2_C2","main")}},
        "R5":{"_id":"R5","Reactants":[[1,"C3"],[1,"Z9"]],"Products":[[1,"C4"]],"RPair":{"RP6":("C3_C4","main"),"RP7":("Z9_C4","trans")}},
        "R6":{"_id":"R6","Reactants":[[1,"C9"]],"Products":[[1,"C8"],[1,"C3"]],"RPair":{"RP8":("C9_C3","main")}},
        "R7":{"_id":"R7","Reactants":[[1,"X4"]],"Products":[[1,"Z4"]],"RPair":{"RP9":("X4_Z4","main")}}
    }
    expected_comp_dict = {
        "C1":{"_id":"C1","Reactions":["R1","R2"],"Formula":"C10H18O2","Reactant_in":["R1"]},
        "C2":{"_id":"C2","Reactions":["R3","R4"],"Formula":"XeF4"},
        "C3":{"_id":"C3","Reactions":["R5","R6"],"Formula":"C10H12O3","Reactant_in":["R5"],"Product_of":["R6"]},
        "C4":{"_id":"C4","Reactions":["R5","RX"],"Formula":"C2H5O","Product_of":["R5"]},
        "C5":{"_id":"C5","Reactions":["R7"],"Formula":"CH3O"},
        "C6":{"_id":"C6","Formula":"C12"}
    }
    SortKeggReactions(kegg_comp_dict, kegg_rxn_dict) # Modifies the kegg_comp_dict directly
    assert kegg_comp_dict == expected_comp_dict

    # How about a real example?
    # Butanol (C06142)
    kegg_comp_ids = ["C01412","C00005","C00080","C06142","C00006","C00004","C00003"]
    kegg_rxn_ids = ["R03545","R03544"]
    kegg_comp_dict = dict(zip(kegg_comp_ids, [FormatKeggCompound(GetKeggText(x)) for x in kegg_comp_ids]))
    kegg_rxn_dict = dict(zip(kegg_rxn_ids, [FormatKeggReaction(GetKeggText(x)) for x in kegg_rxn_ids]))

    expected_comp_dict = deepcopy(kegg_comp_dict)
    expected_comp_dict['C06142']['Product_of'] = ['R03544','R03545']
    expected_comp_dict['C01412']['Reactant_in'] = ['R03544','R03545']

    SortKeggReactions(kegg_comp_dict, kegg_rxn_dict)
    assert kegg_comp_dict == expected_comp_dict


def GetKeggComps(comp_id_list, num_workers=128):
    """
    Threaded implementation of GetKeggText and FormatKeggCompound,
    taking a list of KEGG compound ids as input.
    """
    def Worker():
        while True:
            comp_id = work.get()
            if comp_id is None:
                break
            sWrite("\rHandling compound query '%s'." % str(comp_id))
            output.put(FormatKeggCompound(GetKeggText(comp_id)))
            work.task_done()

    work = queue.Queue()
    output = queue.Queue()

    threads = []

    for i in range(num_workers):
        t = threading.Thread(target=Worker)
        t.start()
        threads.append(t)

    for comp_id in comp_id_list:
        work.put(comp_id)

    # Block until all work is done
    work.join()

    # Stop workers
    for i in range(num_workers):
        work.put(None)
    for t in threads:
        t.join()

    # Get the results
    comps = []

    while not output.empty():
        comps.append(output.get())

    return comps

def test_GetKeggComps():
    comp_ids = ["C04625","C13929","C10269","C05119","C02419"]
    comps_1 = [FormatKeggCompound(GetKeggText(x)) for x in comp_ids]
    comps_2 = GetKeggComps(comp_ids)
    assert len(comps_1) == len(comps_2)
    for comp in comps_1:
        assert comp in comps_2
    for comp in comps_2:
        assert comp in comps_1


def GetKeggRxns(rxn_id_list, num_workers=128):
    """
    Threaded implementation of GetKeggText and FormatKeggReaction,
    taking a list of KEGG reaction ids as input.
    """
    def Worker():
        while True:
            rxn_id = work.get()
            if rxn_id is None:
                break
            sWrite("\rHandling reaction query '%s'." % str(rxn_id))
            output.put(FormatKeggReaction(GetKeggText(rxn_id)))
            work.task_done()

    work = queue.Queue()
    output = queue.Queue()

    threads = []

    for i in range(num_workers):
        t = threading.Thread(target=Worker)
        t.start()
        threads.append(t)

    for rxn_id in rxn_id_list:
        work.put(rxn_id)

    # Block until all work is done
    work.join()

    # Stop workers
    for i in range(num_workers):
        work.put(None)
    for t in threads:
        t.join()

    # Get the results
    rxns = []

    while not output.empty():
        rxns.append(output.get())

    return rxns

def test_GetKeggRxns():
    rxn_ids = ["R10430","R07960","R04715","R07211","R10332"]
    rxns_1 = [FormatKeggReaction(GetKeggText(x)) for x in rxn_ids]
    rxns_2 = GetKeggRxns(rxn_ids)
    assert len(rxns_1) == len(rxns_2)
    for rxn in rxns_1:
        assert rxn in rxns_2
    for rxn in rxns_2:
        assert rxn in rxns_1


def GetRawKegg(kegg_comp_ids=[], kegg_rxn_ids=[], krest="http://rest.kegg.jp", n_threads=128, test_limit=0):
    """
    Downloads all KEGG compound (C) and reaction (R) records and formats them
    as MINE database compound or reaction entries. The final output is a tuple
    containing a compound dictionary and a reaction dictionary.

    Alternatively, downloads only a supplied list of compounds and reactions.
    """

    sWrite("\nDownloading KEGG data via %s/...\n" % krest)

    # Acquire list of KEGG compound IDs
    if not len(kegg_comp_ids):
        sWrite("Downloading KEGG compound list...")
        r = rget("/".join([krest,"list","compound"]))
        if r.status_code == 200:
            for line in r.text.split("\n"):
                if line == "": break # The end
                kegg_comp_id = line.split()[0].split(":")[1]
                kegg_comp_ids.append(kegg_comp_id)
        else:
            msg = "Error: Unable to download KEGG rest compound list.\n"
            sys.exit(msg)
        sWrite(" Done.\n")

    # Acquire list of KEGG reaction IDs
    if not len(kegg_rxn_ids):
        sWrite("Downloading KEGG reaction list...")
        r = rget("/".join([krest,"list","reaction"]))
        if r.status_code == 200:
            for line in r.text.split("\n"):
                if line == "": break # The end
                kegg_rxn_id = line.split()[0].split(":")[1]
                kegg_rxn_ids.append(kegg_rxn_id)
        else:
            msg = "Error: Unable to download KEGG rest reaction list.\n"
            sys.exit(msg)
        sWrite(" Done.\n")

    # Limit download length, for testing only
    if test_limit:
        kegg_comp_ids = kegg_comp_ids[0:test_limit]
        kegg_rxn_ids = kegg_rxn_ids[0:test_limit]

    # Download compounds (threaded)
    kegg_comp_dict = {}
    for comp in GetKeggComps(kegg_comp_ids):
        if comp == None:
            continue
        try:
            kegg_comp_dict[comp['_id']] = comp
        except KeyError:
            sError("Warning: KEGG compound '%s' lacks an ID and will be discarded.\n" % str(comp))
            continue

    print("")

    # Download reactions (threaded)
    kegg_rxn_dict = {}
    for rxn in GetKeggRxns(kegg_rxn_ids):
        if rxn == None:
            continue
        try:
            kegg_rxn_dict[rxn['_id']] = rxn
        except KeyError:
            sError("Warning: KEGG compound '%s' lacks an ID and will be discarded.\n" % str(rxn))
            continue

    print("")

    # Re-organize compound reaction listing, taking cofactor role into account
    sWrite("Organizing reaction lists...")
    SortKeggReactions(kegg_comp_dict, kegg_rxn_dict)
    sWrite(" Done.\n")

    sWrite("KEGG download completed.\n")
    return (kegg_comp_dict, kegg_rxn_dict)

def test_GetRawKegg_1():
    # Butanol (C06142)
    kegg_comp_ids = ["C01412","C00005","C00080","C06142","C00006","C00004","C00003"]
    kegg_rxn_ids = ["R03545","R03544"]
    kegg_comp_dict = dict(zip(kegg_comp_ids, [FormatKeggCompound(GetKeggText(x)) for x in kegg_comp_ids]))
    kegg_rxn_dict = dict(zip(kegg_rxn_ids, [FormatKeggReaction(GetKeggText(x)) for x in kegg_rxn_ids]))

    kegg_comp_dict['C06142']['Product_of'] = ['R03544','R03545']
    kegg_comp_dict['C01412']['Reactant_in'] = ['R03544','R03545']

    assert GetRawKegg(kegg_comp_ids, kegg_rxn_ids) == (kegg_comp_dict, kegg_rxn_dict)

def test_GetRawKegg_2():
    # Random sample
    random_comp_ids = [
    "C14978","C01268","C09868","C05562","C08104",
    "C15636","C14337","C00988","C08400","C19305",
    "C07495","C09986","C04144","C06578","C00508",
    "C17617","C10048","C16549","C04299","C18093"
    ]

    random_rxn_ids = []

    for comp_id in random_comp_ids:
        try:
            random_rxn_ids.extend(KeggRestDict(GetKeggText(comp_id))['REACTION'])
        except KeyError:
            continue

    random_comp_dict = dict(zip(random_comp_ids, [FormatKeggCompound(GetKeggText(x)) for x in random_comp_ids]))
    random_rxn_dict = dict(zip(random_rxn_ids, [FormatKeggReaction(GetKeggText(x)) for x in random_rxn_ids]))
    SortKeggReactions(random_comp_dict, random_rxn_dict)

    assert GetRawKegg(random_comp_ids, random_rxn_ids) == (random_comp_dict, random_rxn_dict)

def test_GetRawKegg_3():
    # First 20 compounds and reactions
    first_comp_ids = [x.split("\t")[0].split(":")[1] for x in rget("http://rest.kegg.jp/list/compound").text.split("\n")[0:20]]
    first_rxn_ids = [x.split("\t")[0].split(":")[1] for x in rget("http://rest.kegg.jp/list/reaction").text.split("\n")[0:20]]

    first_comp_dict = dict(zip(first_comp_ids, [FormatKeggCompound(GetKeggText(x)) for x in first_comp_ids]))
    first_rxn_dict = dict(zip(first_rxn_ids, [FormatKeggReaction(GetKeggText(x)) for x in first_rxn_ids]))
    SortKeggReactions(first_comp_dict, first_rxn_dict)

    assert GetRawKegg(test_limit=20) == (first_comp_dict, first_rxn_dict)


def QuickSearch(con, db, query):
    """Wrapper for MineClient3 quick_search() with reconnect functionality."""
    n = 0
    results = []
    while True:
        try:
            results = con.quick_search(db, query)
            return results
        except mc.ServerError:
            return results
        except:
            # Server not responding, try again
            n += 1
            if n % 5 == 0:
                sError("Warning: Server not responding after %s attempts ('%s').\n" % (str(n), query))
            if n >= 36:
                sError("Warning: Connection attempt limit reached. Returning empty list.\n")
                return results
            if n <= 12:
                time.sleep(10)
            if n > 12:
                time.sleep(30)

def test_QuickSearch():

    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    assert QuickSearch(con, db, 'C00022')[0]['Names'][0] == 'Pyruvate'
    assert QuickSearch(con, db, 'random_query') == []


def ThreadedQuickSearch(con, db, query_list):
    """Threaded implementation of QuickSearch, taking a list of queries as input."""
    def Worker():
        while True:
            query = work.get()
            if query is None:
                break
            sWrite("\rHandling quick query '%s'." % str(query))
            output.put(QuickSearch(con, db, query))
            work.task_done()

    work = queue.Queue()
    output = queue.Queue()

    threads = []
    num_workers = 128

    for i in range(num_workers):
        t = threading.Thread(target=Worker)
        t.start()
        threads.append(t)

    for query in query_list:
        work.put(query)

    # Block until all work is done
    work.join()

    # Stop workers
    for i in range(num_workers):
        work.put(None)
    for t in threads:
        t.join()

    # Get the results
    results = []

    while not output.empty():
        results.extend(output.get())

    return results

def test_ThreadedQuickSearch():
    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    assert ThreadedQuickSearch(con, db, ['C00022'])[0]['Names'][0] == 'Pyruvate'
    assert ThreadedQuickSearch(con, db, ['random_query']) == []
    assert len(ThreadedQuickSearch(con, db, ['C00022','C01719','C13842','C00231'])) == 4


def GetComp(con, db, comp_id):
    """Wrapper for MineClient3 get_comps() with reconnect functionality."""
    n = 0
    while True:
        try:
            results = con.get_comps(db, [comp_id])
            break
        except mc.ServerError:
            results = None
        except:
            # Server not responding, try again
            n += 1
            if n % 5 == 0:
                sError("Warning: Server not responding after %s attempts ('%s').\n" % (str(n), comp_id))
            if n >= 36:
                sError("Warning: Connection attempt limit reached. Results negative.\n")
                results = None
            if n <= 12:
                time.sleep(10)
            if n > 12:
                time.sleep(30)
    try:
        results = results[0]
    except IndexError or TypeError:
        results = None
    if results == None:
        sError("Warning: '%s' could not be retrieved from the database.\n" % comp_id)
    return results

def test_GetComp():
    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    assert GetComp(con, db, 'Cc93137cc81324a5b2872b0bf1c77866c234d66e1')['Formula'] == 'C7H15O10P'
    assert GetComp(con, db, 'Cc93137cc81324a5b2872b0bf1c77866c234d66e1')['dG_error'] == 1.02079
    assert GetComp(con, db, 'not_a_comp_id') == None


def ThreadedGetComps(con, db, comp_id_list):
    """Threaded implementation of GetComp, taking a list of compound ids as input."""
    def Worker():
        while True:
            comp_id = work.get()
            if comp_id is None:
                break
            sWrite("\rHandling compound query '%s'." % str(comp_id))
            output.put(GetComp(con, db, comp_id))
            work.task_done()

    work = queue.Queue()
    output = queue.Queue()

    threads = []
    num_workers = 128

    for i in range(num_workers):
        t = threading.Thread(target=Worker)
        t.start()
        threads.append(t)

    for comp_id in comp_id_list:
        work.put(comp_id)

    # Block until all work is done
    work.join()

    # Stop workers
    for i in range(num_workers):
        work.put(None)
    for t in threads:
        t.join()

    # Get the results
    comps = []

    while not output.empty():
        comps.append(output.get())

    return comps

def test_ThreadedGetComps():
    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    comp_ids = ['C1bb250660ea917ddaa2b2777b4773facd6bebb33',
    'C9effc25891ed5be2d4e0804f72e7c78f24e08825',
    'Ce0b888f73c8eabf45289f3fd8e564ff0a92f0014',
    'Cee14c71f197998d923eefb144761a1626a87b738',
    'C6efa5f2bc583af46e2f0c53f112c875abc916d37']

    comps = [con.get_comps(db, [comp_id])[0] for comp_id in comp_ids]

    comps_t = ThreadedGetComps(con, db, comp_ids)

    elements_identical = True

    for e in comps:
        if not e in comps_t:
            elements_identical = False
    for e in comps_t:
        if not e in comps:
            elements_identical = False

    assert elements_identical


def GetRxn(con, db, rxn_id):
    """Wrapper for MineClient3 get_rxns() with reconnect functionality."""
    n = 0
    while True:
        try:
            results = con.get_rxns(db, [rxn_id])
            break
        except mc.ServerError:
            results = None
        except:
            # Server not responding, try again
            n += 1
            if n % 5 == 0:
                sError("Warning: Server not responding after %s attempts ('%s').\n" % (str(n), rxn_id))
            if n >= 36:
                sError("Warning: Connection attempt limit reached. Results negative.\n")
                results = None
            if n <= 12:
                time.sleep(10)
            if n > 12:
                time.sleep(30)
    try:
        results = results[0]
    except IndexError or TypeError:
        results = None
    if results == None:
        sError("Warning: '%s' could not be retrieved from the database.\n" % rxn_id)
    return results


def test_GetRxn():

    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    rxn_id = 'Re598257045ae3ce45dabf450b57708d84e642558'
    rxn_op = '1.14.13.e'
    rxn_rlen = 4

    assert type(GetRxn(con, db, rxn_id)) == dict
    assert GetRxn(con, db, rxn_id)['Operators'] == [rxn_op]
    assert len(GetRxn(con, db, rxn_id)['Reactants']) == 4
    assert GetRxn(con, db, 'random_reaction') == None


def ThreadedGetRxns(con, db, rxn_id_list):
    """Threaded implementation of GetRxn, taking a list of reaction ids as input."""
    def Worker():
        while True:
            rxn_id = work.get()
            if rxn_id is None:
                break
            sWrite("\rHandling reaction query '%s'." % str(rxn_id))
            output.put(GetRxn(con, db, rxn_id))
            work.task_done()

    work = queue.Queue()
    output = queue.Queue()

    threads = []
    num_workers = 128

    for i in range(num_workers):
        t = threading.Thread(target=Worker)
        t.start()
        threads.append(t)

    for rxn_id in rxn_id_list:
        work.put(rxn_id)

    # Block until all work is done
    work.join()

    # Stop workers
    for i in range(num_workers):
        work.put(None)
    for t in threads:
        t.join()

    # Get the results
    rxns = []

    while not output.empty():
        rxns.append(output.get())

    return rxns

def test_ThreadedGetRxns():
    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    rxn_ids = ['R39b3f701d4b0949c38469e31ef675cb7ca1b0fde',
    'R62503c9b0dab64629bea90753f557c451ad5a9b1',
    'Rec4bc0816e3e97672c93e81bee581f6710eac00f',
    'Rc72c92a8ea137cdcc3ada34dc2589553f94faf20',
    'Rc1015bf465307226440d0692919c708e8d38cfb1',
    'R47a4684b398ad812c44c5eae69b34972f8a4b624',
    'R1d52cfafb75c8fc3f5dbdbc681c623a02b4014f7']

    rxns = [con.get_rxns(db, [rxn_id])[0] for rxn_id in rxn_ids]

    rxns_t = ThreadedGetRxns(con, db, rxn_ids)

    elements_identical = True

    for e in rxns:
        if not e in rxns_t:
            elements_identical = False
    for e in rxns_t:
        if not e in rxns:
            elements_identical = False

    assert elements_identical


def ReadCompounds(filename):
    """Read a file with KEGG compound IDs."""
    sWrite("\nReading compound ID file...\n")
    compounds = [line.rstrip() for line in open(filename, 'r')]
    for c in compounds:
        if re.fullmatch("^C[0-9]{5}$", c) == None:
            msg = "Warning: The supplied string '", c, "' is not a valid KEGG compound ID."
            sys.exit(msg)
    print("Done.")
    return compounds

def test_ReadCompounds():

    import pytest
    import tempfile

    t1 = str.encode("C10000\nC40055\nC13482\n")
    t2 = str.encode("C13854\nR10309\nC33190\n")

    f1 = tempfile.NamedTemporaryFile()
    f1.write(t1)
    f1.flush()
    f2 = tempfile.NamedTemporaryFile()
    f2.write(t2)
    f2.flush()

    assert set(ReadCompounds(f1.name)) == set(['C10000','C40055','C13482'])
    with pytest.raises(SystemExit): ReadCompounds(f2.name)

    f1.close()
    f2.close()


def KeggToMineId(kegg_ids):
    """Translate KEGG IDs to MINE IDs."""
    sWrite("\nTranslating from KEGG IDs to MINE IDs...\n")
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"
    kegg_id_dict = {}
    for kegg_comp in ThreadedGetComps(con, db, [x['_id'] for x in ThreadedQuickSearch(con, db, kegg_ids)]):
        for kegg_id in kegg_comp['DB_links']['KEGG']:
            kegg_id_dict[kegg_id] = kegg_comp['_id']
    for kegg_id in kegg_ids:
        try:
            kegg_comp = kegg_id_dict[kegg_id]
        except KeyError:
            sError("Warning: '%s' is not present in the database.\n" % kegg_id)
            continue
    print("\nDone.\n")
    return kegg_id_dict

def test_KeggToMineId():
    assert KeggToMineId(['C15667', 'C16519', 'C00130']) == {'C15667':'C023e725c27a385fd9057c8171ca1992a32a3e9a4',
    'C16519':'Cfa3c20a209e12ac4a70f10530cf43bea2b4422fe',
    'C00130':'Cae5be0a0a2bb6b6baef29e4d4c6b3f4c1a67ad19'}
    # C00003 is not present in the database
    assert KeggToMineId(['C00003', 'C16519', 'C00130']) == {'C16519':'Cfa3c20a209e12ac4a70f10530cf43bea2b4422fe',
    'C00130':'Cae5be0a0a2bb6b6baef29e4d4c6b3f4c1a67ad19'}


def ExtractReactionCompIds(rxn):
    """Extracts all compound IDs (reactants and products) from a MINE reaction object."""

    rxn_comp_ids = []

    # Get reaction ID and test if the reaction is valid
    try:
        rxn_id = rxn['_id']
    except KeyError:
        sError("Warning: '%s' does not have a reaction ID.\n" % str(rxn))
        rxn_id = 'UnknownReaction'
    except TypeError:
        sError("Warning: '%s' is not a valid reaction.\n" % str(rxn))
        return rxn_comp_ids

    # Try to get the reactants
    try:
        rxn_p = rxn['Reactants']
        try:
            rxn_comp_ids.extend([x[1] for x in rxn_p])
        except IndexError:
            sError("Warning: The reactant list of '%s' is not valid.\n" % rxn_id)
    except KeyError:
        sError("Warning: '%s' does not list its reactants.\n" % rxn_id)

    # Try to get the products
    try:
        rxn_p = rxn['Products']
        try:
            rxn_comp_ids.extend([x[1] for x in rxn_p])
        except IndexError:
            sError("Warning: The product list of '%s' is not valid.\n" % rxn_id)
    except KeyError:
        sError("Warning: '%s' does not list its products.\n" % rxn_id)

    return rxn_comp_ids

def test_ExtractReactionCompIds(capsys):
    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    rxn1 = {'_id':'R1','Products':[[1,'C1'],[1,'C2']],'Reactants':[[1,'X1'],[1,'X2']]}
    rxn2_id = 'Re598257045ae3ce45dabf450b57708d84e642558'
    rxn2 = con.get_rxns(db, [rxn2_id])[0]
    rxn3 = {'_id':'R3','Reactants':[['XZ']]}

    assert set(ExtractReactionCompIds(rxn1)) == set(['C1', 'C2', 'X1', 'X2'])
    assert set(ExtractReactionCompIds(rxn2)) == set([x[1] for x in rxn2['Products']] + [x[1] for x in rxn2['Reactants']])

    ExtractReactionCompIds(rxn3)
    out, err = capsys.readouterr()
    assert err == "Warning: The reactant list of 'R3' is not valid.\nWarning: 'R3' does not list its products.\n"


def LimitCarbon(comp, C_limit=25):
    """Returns True if the compound exceeds the carbon atom limit, otherwise False."""
    regex = re.compile('C{1}[0-9]*')
    try:
        formula = comp['Formula']
    except KeyError:
        try:
            comp_id = comp['_id']
        except KeyError:
            comp_id = 'UnknownCompound'
        sError("Warning: Compound '%s' lacks a formula and will pass the C limit." % (comp_id))
        return False
    match = re.search(regex, formula)
    if match:
        try:
            C_count = int(match.group(0).split('C')[1])
        except ValueError:
            C_count = 1
    else:
        C_count = 0
    if C_count > C_limit:
        return True
    else:
        return False

def test_LimitCarbon():
    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    rxn = con.get_rxns(db, ['R180569d0b4cec9c8392f78015bf8d5341ca05c66'])[0]

    test_1_25 = []
    test_1_50 = []
    for comp in [con.get_comps(db, [comp_id])[0] for comp_id in ExtractReactionCompIds(rxn)]:
        test_1_25.append(LimitCarbon(comp, 25))
        test_1_50.append(LimitCarbon(comp, 50))

    assert True in test_1_25
    assert True not in test_1_50

    rxn = con.get_rxns(db, ['R25b1c5f3ec86899ccbd244413c5e53140c626646'])[0]

    test_2_def = []
    test_2_20 = []
    for comp in [con.get_comps(db, [comp_id])[0] for comp_id in ExtractReactionCompIds(rxn)]:
        test_2_def.append(LimitCarbon(comp))
        test_2_20.append(LimitCarbon(comp, 20))

    assert True not in test_2_def
    assert True in test_2_20


def ExtractCompReactionIds(comp):
    """Extracts all reaction IDs from a MINE compound object."""
    rxn_id_list = []
    try:
        rxn_id_list.extend(comp['Reactant_in'])
    except KeyError:
        pass
    try:
        rxn_id_list.extend(comp['Product_of'])
    except KeyError:
        pass
    return rxn_id_list


def test_ExtractCompReactionIds():
    C1 = {'_id':'C1', 'Reactant_in':['R1']}
    C2 = {'_id':'C1', 'Reactant_in':['R2'], 'Product_of':['R3']}
    C3 = {'_id':'C1', 'Product_of':['R3', 'R4']}
    C4 = {'_id':'C4'}
    assert ExtractCompReactionIds(C1) == ['R1']
    assert ExtractCompReactionIds(C2) == ['R2','R3']
    assert ExtractCompReactionIds(C3) == ['R3','R4']
    assert ExtractCompReactionIds(C4) == []


def GetRawNetwork(comp_id_list, step_limit=10, comp_limit=100000, C_limit=25):
    """Download connected reactions and compounds up to the limits."""

    sWrite("\nDownloading raw network data...\n\n")

    # Set up connection
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    con = mc.mineDatabaseServices(server_url)
    db = "KEGGexp2"

    # Set up output dictionaries
    comp_dict = {}
    rxn_dict = {}

    # Set up counters
    steps = 0
    comps = 0

    # First add the starting compounds
    for comp in ThreadedGetComps(con, db, comp_id_list):
        if comp == None:
            continue
        try:
            comp_id = comp['_id']
        except KeyError:
            sError("Warning: '%s' is not a valid compound.\n" % str(comp))
            continue
        if not LimitCarbon(comp, C_limit):
            comp_dict[comp_id] = comp # Add compound to dict
            comps += 1
        else:
            sError("Warning: Starting compound '%s' exceeds the C limit and is excluded.\n" % comp_id)

    sWrite("\nStep %s finished at %s compounds.\n" % (str(steps), str(comps)))

    extended_comp_ids = set()
    rxn_exceeding_C_limit = set()
    comp_exceeding_C_limit = set()
    comp_cache = {}

    # Perform stepwise expansion of downloaded data
    while steps < step_limit:
        # A new step begins
        steps += 1
        print("")

        # Get the unexplored compounds by subtracting explored from all that are stored
        unextended_comp_ids = set(comp_dict.keys()) - extended_comp_ids

        # Go through each unexplored compound and get a list of the reactions that need to be downloaded
        rxn_ids_to_download = set()

        for comp_id in unextended_comp_ids:
            comp = comp_dict[comp_id] # New compounds are always in the dictionary
            # Get a list of the reactions that the compound is involved in
            rxn_id_list = ExtractCompReactionIds(comp)
            # Go through each reaction
            for rxn_id in rxn_id_list:
                # Reactions that are not in the reaction dictionary
                # and do not exceed the C limit will be downloaded and further explored
                if rxn_id not in rxn_dict.keys() and rxn_id not in rxn_exceeding_C_limit:
                    rxn_ids_to_download.add(rxn_id)

        # Download new rxns
        new_rxns = ThreadedGetRxns(con, db, list(rxn_ids_to_download))
        print("")

        # Go through the downloaded reactions and get a list of compounds to download
        comp_ids_to_download = set()

        for rxn in new_rxns:
            if rxn == None: continue
            rxn_comp_ids = ExtractReactionCompIds(rxn)
            for rxn_comp_id in rxn_comp_ids:
                # Compounds that are not in the reaction dictionary
                # and do not exceed the C limit will be downloaded and further explored
                if rxn_comp_id not in comp_dict.keys() and rxn_comp_id not in comp_cache.keys() and rxn_comp_id not in comp_exceeding_C_limit:
                    comp_ids_to_download.add(rxn_comp_id)

        # Download new compounds
        new_comps = ThreadedGetComps(con, db, list(comp_ids_to_download))

        # Expand the comp_cache with the new compounds
        for comp in new_comps:
            if comp == None: continue
            try:
                comp_id = comp['_id']
            except KeyError:
                sError("Warning: Compound '%s' lacks an ID and will be skipped.\n" % str(comp))
                continue
            comp_cache[comp_id] = comp

        # Go through each new reaction and its compounds
        for rxn in new_rxns:
            new_rxn_comp_ids = set()
            if rxn == None: continue
            try:
                rxn_id = rxn['_id']
            except KeyError:
                sError("Warning: Reaction '%s' lacks an ID and will be skipped.\n" % str(rxn))
                continue
            rxn_comp_ids = ExtractReactionCompIds(rxn)
            for rxn_comp_id in rxn_comp_ids:
                if rxn_comp_id not in comp_dict.keys():
                    # The compound has not been added to the compound dict
                    try:
                        rxn_comp = comp_cache[rxn_comp_id]
                        if LimitCarbon(rxn_comp, C_limit):
                            # The compound and the reaction both exceed the C limit
                            comp_exceeding_C_limit.add(rxn_comp_id)
                            rxn_exceeding_C_limit.add(rxn_id)
                            # We don't want to explore this reaction further and thus break
                            break
                        # The compound passed the C limit
                        new_rxn_comp_ids.add(rxn_comp_id)
                    except KeyError:
                        # The compound was never downloaded
                        continue
            # We've made it through the compounds of the reaction
            if rxn_id in rxn_exceeding_C_limit:
                continue
            # The reaction did not exceed the C limit, so let's harvest the new compounds
            rxn_dict[rxn_id] = rxn # The reaction should also be placed in the reaction dictionary
            for new_rxn_comp_id in new_rxn_comp_ids:
                comp_dict[new_rxn_comp_id] = comp_cache[new_rxn_comp_id]
                comps += 1
                # Stop at compound limit here
                if comps >= comp_limit:
                    sWrite("\nStep %s finished at %s compounds.\n" % (str(steps), str(comps)))
                    print("\nDone.")
                    return (comp_dict, rxn_dict)
        # All reactions in the current step have been explored
        extended_comp_ids = extended_comp_ids.union(unextended_comp_ids)
        sWrite("\nStep %s finished at %s compounds.\n" % (str(steps), str(comps)))
    print("\nDone.")
    return (comp_dict, rxn_dict)

def test_GetRawNetwork():

    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    db = "KEGGexp2"
    con = mc.mineDatabaseServices(server_url)

    rxn_id_list = ['R04759e864c86cfd0eaeb079404d5f18dae6c7227', 'Re598257045ae3ce45dabf450b57708d84e642558']

    c_id_1 = [a for b in [[y[1] for y in rxn['Reactants']] + [y[1] for y in rxn['Products']] for rxn in con.get_rxns(db,rxn_id_list)] for a in b]

    for comp in con.get_comps(db, c_id_1):
        try:
            rxn_id_list.extend(comp['Reactant_in'])
        except KeyError:
            pass
        try:
            rxn_id_list.extend(comp['Product_of'])
        except KeyError:
            pass

    c_id_2 = [a for b in [[y[1] for y in rxn['Reactants']] + [y[1] for y in rxn['Products']] for rxn in con.get_rxns(db,rxn_id_list)] for a in b]

    rxn_id_list = list(set(rxn_id_list))
    c_id_2 = list(set(c_id_2))

    a = con.get_comps(db, c_id_2[0:50])
    b = con.get_comps(db, c_id_2[50:100])
    c = con.get_comps(db, c_id_2[100:])
    comps = a + b + c

    comp_dict = dict([(comp['_id'], comp) for comp in comps])
    rxn_dict = dict([(rxn['_id'], rxn) for rxn in con.get_rxns(db, rxn_id_list)])

    compound_ids = ['Cefbaa83ea06e7c31820f93c1a5535e1378aba42b','C38a97a9f962a32b984b1702e07a25413299569ab']

    assert GetRawNetwork(compound_ids, step_limit=2, C_limit=500) == (comp_dict, rxn_dict)

    # NAD+ should not be connected via reactions
    nad_plus = 'Xf5dc8599a48d0111a3a5f618296752e1b53c8d30'
    nad_comp = con.get_comps(db, [nad_plus])[0]

    assert GetRawNetwork([nad_plus], C_limit=500) == ({nad_plus : nad_comp}, {})

    # Huge compounds are not allowed to grow
    huge = 'Caf6fc55862387e5fd7cd9635ef9981da7f08a531'
    huge_comp = con.get_comps(db, [huge])[0]

    assert GetRawNetwork([huge]) == ({}, {})

    # Using octanol to test the carbon limit
    octanol = 'Cf6baa9f91035ac294770d5e0bfbe039e5ab67261'
    C24_comp = 'C479f661686a597fa18f69c533438aa7bf0e1fd89' # This is connected by 1 step

    net_C25 = GetRawNetwork([octanol], 1)
    net_C20 = GetRawNetwork([octanol], 1, C_limit=20)

    try:
        x = net_C25[0][C24_comp]['_id']
    except KeyError:
        x = 1
    try:
        y = net_C20[0][C24_comp]['_id']
    except KeyError:
        y = 1

    assert x == C24_comp
    assert y == 1


def AddCompoundNode(graph, compound, start_comp_ids):
    """Adds a compound node to the graph."""
    N = len(graph.nodes()) + 1
    try:
        mid = compound['_id']
    except:
        sError("Warning: Compound '%s' is malformed and will not be added to the network.\n" % str(compound))
        return graph
    if mid in start_comp_ids:
        start = True
    elif 'C' + mid[1:] in start_comp_ids:
        start = True
    elif mid[0] == 'X' and not LimitCarbon(compound, 0):
        # Make sure that inorganic X compounds are treated as starting compounds
        start = True
    else:
        start = False
    graph.add_node(N, type='c', mid=mid, start=start)
    # Also add a mid to node entry in the graph dictionary
    try:
        graph.graph['cmid2node'][mid] = N
    except KeyError:
        graph.graph['cmid2node'] = {mid : N}
    return graph

def test_AddCompoundNode():
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    db = "KEGGexp2"
    con = mc.mineDatabaseServices(server_url)

    G1 = nx.DiGraph()
    G2 = nx.DiGraph()
    comp1 = con.get_comps(db, ['Xf5dc8599a48d0111a3a5f618296752e1b53c8d30'])[0]
    comp2 = con.get_comps(db, ['C38a97a9f962a32b984b1702e07a25413299569ab'])[0]
    comp3 = con.get_comps(db, ['X96ff2c653c25b4f3c6fab12b241ec78bff13a751'])[0] # Phosphate - not listed as start, no carbon
    comp4 = con.get_comps(db, ['C89b394fd02e5e5e60ae1e167780ea7ab3276288e'])[0]

    G2.add_node(1, type='c', mid=comp1['_id'], start=True)
    G2.add_node(2, type='c', mid=comp2['_id'], start=False)
    G2.add_node(3, type='c', mid=comp3['_id'], start=True)
    G2.add_node(4, type='c', mid=comp4['_id'], start=True)

    sids = set(['Cf5dc8599a48d0111a3a5f618296752e1b53c8d30', 'C89b394fd02e5e5e60ae1e167780ea7ab3276288e']) # Note that the X has been replaced with C

    for comp in [comp1, comp2, comp3, comp4]:
        AddCompoundNode(G1, comp, sids)

    assert nx.is_isomorphic(G1, G2)

    assert G1.node[1]['mid'] == G2.node[1]['mid']
    assert G1.node[2]['mid'] == G2.node[2]['mid']
    assert G1.node[3]['mid'] == G2.node[3]['mid']
    assert G1.node[4]['mid'] == G2.node[4]['mid']

    assert G1.node[1]['start'] == G2.node[1]['start'] == True
    assert G1.node[2]['start'] == G2.node[2]['start'] == False
    assert G1.node[3]['start'] == G2.node[3]['start'] == True
    assert G1.node[4]['start'] == G2.node[4]['start'] == True

    assert G1.nodes(data=True) == G2.nodes(data=True)

    assert G1.graph['cmid2node'] == {
        'Xf5dc8599a48d0111a3a5f618296752e1b53c8d30':1,
        'C38a97a9f962a32b984b1702e07a25413299569ab':2,
        'X96ff2c653c25b4f3c6fab12b241ec78bff13a751':3,
        'C89b394fd02e5e5e60ae1e167780ea7ab3276288e':4
        }


def CheckConnection(minetwork, c_node, r_node):
    """Checks that the compound-to-reaction node connection is valid."""

    con_check = False

    c_mid = minetwork.node[c_node]['mid']
    r_mid = minetwork.node[r_node]['mid']
    r_type = minetwork.node[r_node]['type']

    if r_type in {'rf','pr'}:
        try:
            if r_mid in minetwork.graph['mine_data'][c_mid]['Reactant_in']:
                con_check = True
        except KeyError:
            pass

    if r_type in {'pf','rr'}:
        try:
            if r_mid in minetwork.graph['mine_data'][c_mid]['Product_of']:
                con_check = True
        except KeyError:
            pass

    return con_check

def test_CheckConnection(capsys):
    G = nx.DiGraph(
    mine_data = {'C1':{'_id':'C1','Reactant_in':['R1']}, 'C2':{'_id':'C2','Reactant_in':['R1']}, 'R1':{'_id':'R1', 'Reactants':[[1,'C1'],[1,'C2']], 'Products':[[1,'C3']]}, 'C3':{'_id':'C3','Product_of':['R1']}},
    )
    G.add_node(1, type='c', mid='C1')
    G.add_node(2, type='c', mid='C2')
    G.add_node(3, type='rf', mid='R1', c=set([1,2]))
    G.add_node(4, type='pf', mid='R1', c=set([5]))
    G.add_node(5, type='c', mid='C3')
    G.add_node(6, type='rr', mid='R1', c=set([5]))
    G.add_node(7, type='pr', mid='R1', c=set([1,2]))

    assert CheckConnection(G, 1, 3)
    assert CheckConnection(G, 5, 6)

    assert not CheckConnection(G, 2, 4)


def AddQuadReactionNode(graph, rxn):
    """
    Adds a "Quad Reaction Node" (QRN) group of nodes to a graph, and connects
    them to the correct compound nodes.

    The QRN consists of two nodes constituting the intended forward direction
    of the reaction and two nodes constituting the reverse direction. Each pair
    of nodes is connected by an edge in the direction of the reaction. Each node
    represents a group of compounds on one side of the reaction equation.
    """

    # Make sure the reaction is in good shape

    rxn_malformed = False

    try:
        rxn_id = rxn['_id']
    except:
        rxn_malformed = True

    try:
        reactants_f = set([x[1] for x in rxn['Reactants']])
        products_f = set([x[1] for x in rxn['Products']])
        reactants_r = products_f
        products_r = reactants_f
    except:
        rxn_malformed = True

    if rxn_malformed:
        sError("Warning: Reaction '%s' is malformed and will not be added to the network.\n" % str(rxn))
        return graph

    # Find the compound nodes of the reactants and the products
    rf = set([])
    pf = set([])
    rr = set([])
    pr = set([])

    for c_mid in reactants_f:
        try:
            node = graph.graph['cmid2node'][c_mid]
            rf.add(node)
            pr.add(node)
        except KeyError:
            # If a reactant is missing, the reaction should not be added
            sError("Warning: Compound '%s' in reaction '%s' is missing. Reaction nodes were not added to the network.\n" % (c_mid, rxn_id))
            return graph
    for c_mid in products_f:
        try:
            node = graph.graph['cmid2node'][c_mid]
            pf.add(node)
            rr.add(node)
        except KeyError:
            # If a product is missing, the reaction should not be added
            sError("Warning: Compound '%s' in reaction '%s' is missing. Reaction nodes were not added to the network.\n" % (c_mid, rxn_id))
            return graph

    # Create the reaction nodes
    N = len(graph.nodes()) + 1

    graph.add_node(N, type='rf', mid=rxn_id, c=rf)
    for c_node in rf:
        if CheckConnection(graph, c_node, N):
            graph.add_edge(c_node, N)

    N += 1

    graph.add_node(N, type='pf', mid=rxn_id, c=pf)
    for c_node in pf:
        if CheckConnection(graph, c_node, N):
            graph.add_edge(N, c_node)

    graph.add_edge(N-1, N) # Forward reaction edge

    N += 1

    graph.add_node(N, type='rr', mid=rxn_id, c=rr)
    for c_node in rr:
        if CheckConnection(graph, c_node, N):
            graph.add_edge(c_node, N)

    N += 1

    graph.add_node(N, type='pr', mid=rxn_id, c=pr)
    for c_node in pr:
        if CheckConnection(graph, c_node, N):
            graph.add_edge(N, c_node)

    graph.add_edge(N-1, N) # Reverse reaction edge

    return graph

def test_AddQuadReactionNode(capsys):
    server_url = "http://bio-data-1.mcs.anl.gov/services/mine-database"
    db = "KEGGexp2"
    con = mc.mineDatabaseServices(server_url)

    rxn = con.get_rxns(db, ['R04759e864c86cfd0eaeb079404d5f18dae6c7227'])[0]

    r_mid = ['Caf6fc55862387e5fd7cd9635ef9981da7f08a531', 'X25a9fafebc1b08a0ae0fec015803771c73485a61']
    p_mid = ['Cefbaa83ea06e7c31820f93c1a5535e1378aba42b', 'Xf729c487f9b991ec6f645c756cf34b9a20b9e8a4']
    r_node_c = set([1,2])
    p_node_c = set([3,4])

    G1 = nx.DiGraph()
    G1.add_node(1, type='c', mid=r_mid[0], start=False) # r_mid[0] is connected
    G1.add_node(2, type='c', mid=r_mid[1], start=True) # r_mid[1] is ATP; not connected
    G1.add_node(3, type='c', mid=p_mid[0], start=False) # p_mid[0] is connected
    G1.add_node(4, type='c', mid=p_mid[1], start=False) # r_mid[2] is ADP; not connected

    G1.graph['cmid2node'] = {}
    for node in G1.nodes():
        G1.graph['cmid2node'][G1.node[node]['mid']] = node

    rxn_id = 'R04759e864c86cfd0eaeb079404d5f18dae6c7227'

    G1.add_node(5, type='rf', mid=rxn_id, c=r_node_c) # Forward (intended) direction reactants
    G1.add_node(6, type='pf', mid=rxn_id, c=p_node_c) # Forward (intended) direction products
    G1.add_node(7, type='rr', mid=rxn_id, c=p_node_c) # Reverse direction reactants
    G1.add_node(8, type='pr', mid=rxn_id, c=r_node_c) # Reverse direction products
    G1.add_edge(5, 6) # Directed edge for the forward reaction
    G1.add_edge(7, 8) # Directed edge for the reverse reaction

    # Edges connecting compound and reaction nodes
    G1.add_edge(1, 5)
    #G1.add_edge(2, 5) # ATP should not be connected
    G1.add_edge(6, 3)
    #G1.add_edge(6, 4) # ADP should not be connected
    G1.add_edge(3, 7)
    #G1.add_edge(4, 7) # ADP should not be connected
    G1.add_edge(8, 1)
    #G1.add_edge(8, 2) # ATP should not be connected

    G2 = nx.DiGraph(mine_data={
    r_mid[0] : con.get_comps(db, [r_mid[0]])[0],
    r_mid[1] : con.get_comps(db, [r_mid[1]])[0],
    p_mid[0] : con.get_comps(db, [p_mid[0]])[0],
    p_mid[1] : con.get_comps(db, [p_mid[1]])[0]
    })
    G2.add_node(1, type='c', mid=r_mid[0], start=False)
    G2.add_node(2, type='c', mid=r_mid[1], start=True)
    G2.add_node(3, type='c', mid=p_mid[0], start=False)
    G2.add_node(4, type='c', mid=p_mid[1], start=False)

    G2.graph['cmid2node'] = {}
    for node in G2.nodes():
        G2.graph['cmid2node'][G2.node[node]['mid']] = node

    G2 = AddQuadReactionNode(G2, rxn)

    assert nx.is_isomorphic(G1, G2)
    assert G1.nodes(data=True) == G2.nodes(data=True)

    r1 = {'_id':'R1','Reactants':[[1,'C1'],[1,'X1']],'Products':[[1,'C2'],[1,'X2']]}
    r2 = {'_id':'R2','Reactants':[[1,'C2']],'Products':[[1,'C3']]}
    c1 = {'_id':'C1','Reactant_in':['R1']}
    c2 = {'_id':'C2','Product_of':['R1'],'Reactant_in':['R2']}
    c3 = {'_id':'C3','Product_of':['R2']}
    x1 = {'_id':'X1'}
    x2 = {'_id':'X2'}
    G3 = nx.DiGraph(mine_data={'R1':r1,'R2':r2,'C1':c1,'C2':c2,'C3':c3,'X1':x1,'X2':x2})
    G3.add_node(1,mid='C1',type='c')
    G3.add_node(2,mid='C2',type='c')
    G3.add_node(3,mid='C3',type='c')
    G3.add_node(4,mid='X1',type='c')
    G3.add_node(5,mid='X2',type='c')

    G3.graph['cmid2node'] = {}
    for node in G3.nodes():
        G3.graph['cmid2node'][G3.node[node]['mid']] = node

    G3 = AddQuadReactionNode(G3, r1)
    G3 = AddQuadReactionNode(G3, r2)

    assert len(G3.edges()) == 12
    assert len(G3.nodes()) == 13


    G4 = nx.DiGraph()
    G4.add_node(1, type='c', mid=r_mid[0], start=False)
    G4.add_node(2, type='c', mid=r_mid[1], start=True)
    G4.add_node(3, type='c', mid=p_mid[0], start=False)
    # G4.add_node(4, type='c', mid=p_mid[1], start=False) # Skip this node

    G4.graph['cmid2node'] = {}
    for node in G4.nodes():
        G4.graph['cmid2node'][G4.node[node]['mid']] = node

    G4 = AddQuadReactionNode(G4, rxn)

    out, err = capsys.readouterr()
    assert err == "Warning: Compound 'Xf729c487f9b991ec6f645c756cf34b9a20b9e8a4' in reaction 'R04759e864c86cfd0eaeb079404d5f18dae6c7227' is missing. Reaction nodes were not added to the network.\n"
    assert len(G4.edges()) == 0


def ExpandStartCompIds(comp_dict, start_comp_ids, extra_kegg_ids=[]):
    """
    Expands a set of start compound IDs with those of compounds sharing
    the same KEGG ID.
    """
    start_kegg_ids = set(extra_kegg_ids)
    for start_comp_id in start_comp_ids:
        try:
            start_comp = comp_dict[start_comp_id]
        except KeyError:
            # Missing start compound IDs missing is not optimal
            # By-passing them here
            continue
        try:
            start_kegg_ids = start_kegg_ids.union(set(start_comp['DB_links']['KEGG']))
        except KeyError:
            pass
    for comp_id in comp_dict.keys():
        comp = comp_dict[comp_id]
        try:
            if len(set(comp['DB_links']['KEGG']).intersection(start_kegg_ids)):
                start_comp_ids.add(comp_id)
        except KeyError:
            pass
    return start_comp_ids

def test_ExpandStartCompIds():
    comp_dict = {
        'S1':{'_id':'S1','DB_links':{'KEGG':['C00001']}},
        'S2':{'_id':'S2','DB_links':{'KEGG':['C00002','C00003']}},
        'S3':{'_id':'S3','DB_links':{}},
        'S4':{'_id':'S4'},
        'C1':{'_id':'C1','DB_links':{'KEGG':['C00001']}},
        'C2':{'_id':'C2','DB_links':{}},
        'C3':{'_id':'C3'},
        'C4':{'_id':'C4','DB_links':{'KEGG':['C00002']}},
        'X1':{'_id':'X1','DB_links':{'KEGG':['C00002','C10284']}},
        'X5':{'_id':'X5','DB_links':{'KEGG':['C00006','C00007']}},
        'X6':{'_id':'X6','DB_links':{'KEGG':['C11111']}}
    }
    assert ExpandStartCompIds(comp_dict, set(['S1','S2','S3','S4']), ['C11111']) == set(['S1','S2','S3','S4','C1','C4','X1','X6'])


def ConstructNetwork(comp_dict, rxn_dict, start_comp_ids=[], extra_kegg_ids=[]):
    """Constructs a directed graph (network) from the compound and reaction
    dictionaries produced by GetRawNetwork and/or GetRawKegg."""

    sWrite("\nConstructing network...\n")

    # ExpandStartCompIds catches "unlisted" compounds with the same KEGG ID
    start_comp_ids = ExpandStartCompIds(comp_dict, set(start_comp_ids), extra_kegg_ids=extra_kegg_ids)

    # Initialise directed graph
    minetwork = nx.DiGraph(mine_data={**comp_dict, **rxn_dict})

    # Add all compounds
    n_comp = len(comp_dict)
    n_done = 0
    for comp_id in sorted(comp_dict.keys()):
        comp = comp_dict[comp_id]
        minetwork = AddCompoundNode(minetwork, comp, start_comp_ids)
        progress = float(100*n_done/n_comp)
        sWrite("\rAdding compounds... %0.1f%%" % progress)
        n_done += 1

    # Add all reactions
    print("")
    n_rxn = len(rxn_dict)
    n_done = 0
    for rxn_id in sorted(rxn_dict.keys()):
        rxn = rxn_dict[rxn_id]
        minetwork = AddQuadReactionNode(minetwork, rxn)
        progress = float(100*n_done/n_rxn)
        sWrite("\rAdding reactions... %0.1f%%" % progress)
        n_done += 1

    print("\nDone.")
    return minetwork

def test_ConstructNetwork(capsys):
    comp_dict = {}
    comp_dict['C1'] = {'_id':'C1', 'Reactant_in':['R99']}
    comp_dict['C2'] = {'_id':'C2', 'Reactant_in':['R1e'], 'Product_of':['R99']}
    comp_dict['C3'] = {'_id':'C3', 'Reactant_in':['Rcd'], 'Product_of':['R99','Rc3']}
    comp_dict['C4'] = {'_id':'C4', 'Product_of':['R1e']}
    comp_dict['C5'] = {'_id':'C5', 'Reactant_in':['Rc3','R2f'], 'Product_of':['Rcd','R1e']}
    comp_dict['C6'] = {'_id':'C6', 'Product_of':['Rcd']}
    comp_dict['C7'] = {'_id':'C7', 'Product_of':['R2f','R7f']} # Seeding with non-expanded reaction R7f
    comp_dict['C8'] = {'_id':'C8', 'Reactant_in':['Rb7'], 'Product_of':['R2f']} # Seeding with non-expanded reaction Rb7

    rxn_dict = {}
    rxn_dict['R1e'] = {'_id':'R1e', 'Products':[[1,'C4'],[1,'C5']], 'Reactants':[[1,'C2']]} #9
    rxn_dict['R2f'] = {'_id':'R2f', 'Products':[[1,'C7'],[1,'C8']], 'Reactants':[[1,'C5']]} #13
    rxn_dict['R99'] = {'_id':'R99', 'Products':[[1,'C2'],[1,'C3']], 'Reactants':[[1,'C1']]} #17
    rxn_dict['Rc3'] = {'_id':'Rc3', 'Products':[[1,'C3']], 'Reactants':[[1,'C5']]} #21
    rxn_dict['Rcd'] = {'_id':'Rcd', 'Products':[[1,'C5'],[1,'C6']], 'Reactants':[[1,'C3']]} #25

    G = nx.DiGraph(mine_data = {**comp_dict, **rxn_dict})

    start_comp_ids = set(['C1'])

    for comp_id in sorted(comp_dict.keys()):
        comp = comp_dict[comp_id]
        G = AddCompoundNode(G, comp, start_comp_ids)

    N = 8

    for rxn_id in sorted(rxn_dict.keys()):
        rxn = rxn_dict[rxn_id]
        reactants = set([int(x[1][1]) for x in rxn['Reactants']])
        products = set([int(x[1][1]) for x in rxn['Products']])
        N += 1
        G.add_node(N, type='rf', mid=rxn_id, c=reactants)
        N += 1
        G.add_node(N, type='pf', mid=rxn_id, c=products)
        G.add_edge(N-1, N)
        N += 1
        G.add_node(N, type='rr', mid=rxn_id, c=products)
        N += 1
        G.add_node(N, type='pr', mid=rxn_id, c=reactants)
        G.add_edge(N-1, N)

    # C1 edges
    G.add_edge(1, 17)
    G.add_edge(20, 1)

    # C2 edges
    G.add_edge(18, 2)
    G.add_edge(2, 19)
    G.add_edge(2, 9)
    G.add_edge(12, 2)

    # C3 edges
    G.add_edge(18, 3)
    G.add_edge(3, 19)
    G.add_edge(3, 23)
    G.add_edge(22, 3)
    G.add_edge(3, 25)
    G.add_edge(28, 3)

    # C4 edges
    G.add_edge(10, 4)
    G.add_edge(4, 11)

    # C5 edges
    G.add_edge(10, 5)
    G.add_edge(5, 11)
    G.add_edge(24, 5)
    G.add_edge(5, 21)
    G.add_edge(26, 5)
    G.add_edge(5, 27)
    G.add_edge(5, 13)
    G.add_edge(16, 5)

    # C6 edges
    G.add_edge(26, 6)
    G.add_edge(6, 27)

    # C7 edges
    G.add_edge(14, 7)
    G.add_edge(7, 15)

    # C8 edges
    G.add_edge(14, 8)
    G.add_edge(8, 15)

    assert nx.is_isomorphic(ConstructNetwork(comp_dict,rxn_dict,['C1']), G)

    # Test contents node by node
    t = True
    for node in ConstructNetwork(comp_dict,rxn_dict,['C1']).nodes(data=True):
        if G.node[node[0]] != node[1]:
            t = False
            break
    assert t


def IsConnectedMineComp(comp_id, network):
    """Determines if the MINE compound is connected."""
    if set(['Reactant_in','Product_of']).intersection(network.graph['mine_data'][comp_id].keys()):
        return True
    else:
        return False

def test_IsConnectedMineComp():
    G = nx.DiGraph()
    G.graph['mine_data'] = {
        'C1':{'Reactant_in':[]},
        'X2':{},
        'C3':{'Reactant_in':[],'Product_of':[]},
        'C4':{'Product_of':[]},
        'C5':{}
    }
    assert [IsConnectedMineComp(mid,G) for mid in ['C1','X2','C3','C4','C5']] == [True,False,True,True,False]


def KeggMineIntegration(network):
    """
    Integrates KEGG and MINE sub-networks

    Transfers incoming and outgoing edges of KEGG nodes to their matching MINE
    nodes. Integrated KEGG nodes are then removed.

    Does not alter the 'mine_data' dictionary of reactions and compounds.
    """

    sWrite("\nPerforming KEGG/MINE integration...\n")

    # Set up dictionary that lists MINE IDs for KEGG IDs
    sWrite("Setting up KEGG to MINE ID dictionary...")
    kegg_to_mine = {}
    c_node_count = 0
    mc_node_count = 0
    for node in network.nodes():
        if network.node[node]['type'] == 'c':
            c_node_count += 1
            mine_id = network.node[node]['mid']
            mid_match = re.match('^[CX]{1}[0-9,a-f]{40}$', mine_id)
            if mid_match:
                mc_node_count += 1
                try:
                    kegg_ids = set(network.graph['mine_data'][mine_id]['DB_links']['KEGG'])
                except KeyError:
                    continue
                for kegg_id in kegg_ids:
                    try:
                        kegg_to_mine[kegg_id].add(mine_id)
                    except KeyError:
                        kegg_to_mine[kegg_id] = set([mine_id])
    sWrite(" Done.\n")

    # Go through all KEGG compound nodes and perform transplantation
    kc_node_count = c_node_count - mc_node_count
    n = 0
    for node in network.nodes():
        if network.node[node]['type'] == 'c':
            kegg_id = network.node[node]['mid']
            kegg_match = re.match('^C{1}[0-9]{5}$', compound)
            if kegg_match:
                try:
                    mine_ids = kegg_to_mine[kegg_id]
                except KeyError:
                    # No mine_ids found associated with this KEGG node
                    continue
            # Get connected reactant nodes (downstream)
            con_r_nodes = network.successors(node)
            # Get connected product nodes (upstream)
            con_p_nodes = network.predecessors(node)
            # Go through the corresponding MINE nodes
            for mine_id in mine_ids:
                mine_node = network.graph['cmid2node'][mine_id]
                # Transfer connections if the MINE node is connected
                if IsConnectedMineComp(mine_id, network):
                    # Incoming edges
                    network.add_edges_from(zip(con_p_nodes,repeat(mine_node,len(con_p_nodes))))
                    # Outgoing edges
                    network.add_edges_from(zip(repeat(mine_node,len(con_r_nodes),con_r_nodes)))
                # Update KEGG reactant nodes with the new MINE node replacement


                # Update KEGG product nodes with the new MINE node replacement
            # Finally, remove the KEGG node,
            network.remove_node(node)
        n += 1
        progress = float(100 * n / kc_node_count)
        sWrite("\rTransferring edges... %0.1f%%" % progress)
    sWrite("\nIntegration completed.\n")

def test_KeggMineIntegration():
    G = nx.DiGraph()

    # Add KEGG compound nodes
    G.add_node(1,type='c',mid='C00001',start=True) # 'X490c4e...'     A
    G.add_node(2,type='c',mid='C00002',start=False) # 'C683de2...'    B
    G.add_node(3,type='c',mid='C00003',start=False) # 'C069ca5...'    C
    G.add_node(4,type='c',mid='C00004',start=False) # 'C123097...'    D

    # Add MINE compound nodes
    G.add_node(5,type='c',mid='X490c4e9c5d9c3b903bab41ff596eca62ed06130d',start=True) #  A
    G.add_node(6,type='c',mid='C683de2716dd472f4da0a144683d31a10e48a45fc',start=False) # B
    G.add_node(7,type='c',mid='C069ca544492566919b8c9d20984e55b37a9f79a8',start=False) # C
    G.add_node(8,type='c',mid='C123097ef07e00abcd707e873bbd09783da730a38',start=False) # D

    # Add KEGG reaction nodes (A<->C and C<->B)
    G.add_node(9,type='rf',mid='R00001',c=set([1]))
    G.add_node(10,type='pf',mid='R00001',c=set([3]))
    G.add_node(11,type='rr',mid='R00001',c=set([3]))
    G.add_node(12,type='pr',mid='R00001',c=set([1]))
    G.add_path([1,9,10,3])
    G.add_path([3,11,12,1])

    G.add_node(13,type='rf',mid='R00002',c=set([3]))
    G.add_node(14,type='pf',mid='R00002',c=set([2]))
    G.add_node(15,type='rr',mid='R00002',c=set([2]))
    G.add_node(16,type='pr',mid='R00002',c=set([3]))
    G.add_path([3,13,14,2])
    G.add_path([2,15,16,3])

    # Add MINE reaction nodes (Disconnected A<->C and B<->D)
    G.add_node(17,type='rf',mid='Rf2279c67b1b433641502020c3ddd46b911827b88',c=set([5]))
    G.add_node(18,type='pf',mid='Rf2279c67b1b433641502020c3ddd46b911827b88',c=set([7]))
    G.add_node(19,type='rr',mid='Rf2279c67b1b433641502020c3ddd46b911827b88',c=set([7]))
    G.add_node(20,type='pr',mid='Rf2279c67b1b433641502020c3ddd46b911827b88',c=set([5]))
    G.add_path([17,18,7])
    G.add_path([7,19,20])

    G.add_node(21,type='rf',mid='Re9283748451e3dc8254bcd45342926db929b2176',c=set([6]))
    G.add_node(22,type='pf',mid='Re9283748451e3dc8254bcd45342926db929b2176',c=set([8]))
    G.add_node(23,type='rr',mid='Re9283748451e3dc8254bcd45342926db929b2176',c=set([8]))
    G.add_node(24,type='pr',mid='Re9283748451e3dc8254bcd45342926db929b2176',c=set([6]))
    G.add_path([6,21,22,8])
    G.add_path([8,23,24,6])

    # Add mine_data dictionary to network
    G.graph['mine_data'] = {
        'C00001':{"DB_links":{'KEGG':['C00001']},'Reactant_in':['R00001']},
        'X490c4e9c5d9c3b903bab41ff596eca62ed06130d':{"DB_links":{'KEGG':['C00001']}},
        'C00002':{"DB_links":{'KEGG':['C00002']},'Product_of':['R00002']},
        'C683de2716dd472f4da0a144683d31a10e48a45fc':{"DB_links":{'KEGG':['C00002']},'Reactant_in':['Re9283748451e3dc8254bcd45342926db929b2176']},
        'C00003':{"DB_links":{'KEGG':['C00003']},'Reactant_in':['R00002'],'Product_of':['R00001']},
        'C069ca544492566919b8c9d20984e55b37a9f79a8':{"DB_links":{'KEGG':['C00003']},'Product_of':['Rf2279c67b1b433641502020c3ddd46b911827b88']},
        'C00004':{"DB_links":{'KEGG':['C00004']}},
        'C123097ef07e00abcd707e873bbd09783da730a38':{"DB_links":{'KEGG':['C00004']},'Product_of':['Re9283748451e3dc8254bcd45342926db929b2176']},
        'R00001':{},
        'Rf2279c67b1b433641502020c3ddd46b911827b88':{},
        'R00002':{},
        'Re9283748451e3dc8254bcd45342926db929b2176':{}
    }

    # Copy and integrate
    H = G.copy()
    KeggMineIntegration(H)

    # A new path should have been introduced
    assert not nx.has_path(G, 6, 8)
    assert nx.has_path(H, 6, 8)

    # Node 5 should stay disconnected
    assert len(G.predecessors(5)) == len(G.successors(5)) == len(H.predecessors(5)) == len(H.successors(5)) == 0

    # Four nodes should have been removed
    nodes_removed = True
    for node in [1,2,3,4]:
        try:
            x = H.node[node]
        except KeyError:
            continue
        nodes_removed = False
    assert nodes_removed

    # Every reaction node should have the correct c node set
    c_sets = [set([x]) for x in [5,7,7,5,7,6,6,7,5,7,7,5,6,8,8,6]]
    assert sum([H.node[en[0]+9]['c'] == en[1] for en in enumerate(c_sets)]) == 20

    # All connections must be transferred correctly
    expected_edges = set([
        (9,10), (11,12), (13,14), (15,16), (17,18), (19,20), (21,22), (23,24),
        (10,7), (7,11),
        (7,13), (14,6), (6,15), (16,7),
        (18,7), (7,18),
        (6,21), (22,8), (8,23), (24,6)
    ])
    assert set(H.edges()) == expected_edges

# Main code block
def main(infile_name, step_limit, comp_limit, C_limit, outfile_name):
    # Get starting compound MINE IDs
    start_kegg_ids = ReadCompounds(infile_name)
    start_ids = list(set(KeggToMineId(start_kegg_ids).values()))
    # Create the network
    minetwork = ConstructNetwork(*GetRawNetwork(start_ids, step_limit, comp_limit, C_limit), start_ids, extra_kegg_ids=start_kegg_ids)
    # Save to Pickle
    pickle.dump(minetwork, open(outfile_name, 'wb'))


if __name__ == "__main__":
    # Read arguments from the commandline
    parser = argparse.ArgumentParser()
    parser.add_argument('infile', help='Read KEGG compound identifiers from text file.')
    parser.add_argument('outfile', help='Write MINE network to Python Pickle file.')
    parser.add_argument('-r', type=int, default=10, help='Maximum number of reaction steps to download.')
    parser.add_argument('-c', type=int, default=100000, help='Maximum number of compounds to download.')
    parser.add_argument('-C', type=int, default=25, help='Maximum number of C atoms per molecule for following a reaction.')
    args = parser.parse_args()
    main(args.infile, args.r, args.c, args.C, args.outfile)
