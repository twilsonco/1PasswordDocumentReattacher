#!/opt/homebrew/bin/python3
# use default python
# #!/usr/bin/env python3

# Standard library imports
import argparse
import csv
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from collections import defaultdict
from datetime import datetime

# Third party imports
from tqdm import tqdm

OP_CLI_PATH = "/opt/homebrew/bin/op"
DRY_RUN = True
ARCHIVE_DOCS = True
SUPERVISE_RUN = False
VERBOSE = False

def sanitize(filename:str) -> str:
    """Return a fairly safe version of the filename.
    https://gitlab.com/jplusplus/sanitize-filename

    We don't limit ourselves to ascii, because we want to keep municipality
    names, etc, but we do want to get rid of anything potentially harmful,
    and make sure we do not exceed Windows filename length limits.
    Hence a less safe blacklist, rather than a whitelist.
    """
    blacklist = ["\\", "/", ":", "*", "?", "\"", "<", ">", "|", "\0"]
    reserved = [
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
        "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
        "LPT6", "LPT7", "LPT8", "LPT9",
    ]  # Reserved words on Windows
    filename = "".join(c for c in filename if c not in blacklist)
    # Remove all charcters below code point 32
    filename = "".join(c for c in filename if 31 < ord(c))
    filename = unicodedata.normalize("NFKD", filename)
    filename = filename.rstrip(". ")  # Windows does not allow these at end
    filename = filename.strip()
    if all([x == "." for x in filename]):
        filename = "__" + filename
    if filename in reserved:
        filename = "__" + filename
    if len(filename) == 0:
        filename = "__"
    if len(filename) > 255:
        parts = re.split(r"/|\\", filename)[-1].split(".")
        if len(parts) > 1:
            ext = "." + parts.pop()
            filename = filename[:-len(ext)]
        else:
            ext = ""
        if filename == "":
            filename = "__"
        if len(ext) > 254:
            ext = ext[254:]
        maxl = 255 - len(ext)
        filename = filename[:maxl]
        filename = filename + ext
        # Re-check last character (if there was no extension)
        filename = filename.rstrip(". ")
        if len(filename) == 0:
            filename = "__"
    return filename

def R(cmd:str) -> bytes:
    """
    Execute a command using the subprocess.run method and return the output as a string.

    Args:
        cmd (str): The command to be executed.

    Returns:
        str: The output of the executed command.
    """
    # Fails for no reason sometimes, so try a few times
    max_num_attempts = 1
    num_attempts = 0
    while num_attempts < max_num_attempts:
        try:
            return subprocess.run(f"{OP_CLI_PATH} {cmd}", shell=True, check=True, capture_output=True).stdout
        except Exception as e:
            num_attempts += 1
            if num_attempts == max_num_attempts:
                raise e

def J(cmd:str):
    """
    Execute a command and return the output as a JSON object.

    Args:
        cmd (str): The command to be executed.

    Returns:
        dict or list of dicts: A JSON object representing the output of the executed command.
    """
    return json.loads(R(cmd + " --format=json"))

def S(cmd:str) -> str:
    """
    Execute a command using the subprocess.run method and return the output as a string.

    Args:
        cmd (str): The command to be executed.

    Returns:
        str: The output of the executed command.
    """
    return R(cmd).decode("utf-8").strip()

def allowed_by_white_black_lists(s, whitelist=[], blacklist=[], exact_match=False) -> tuple:
    """
    Check if a given string is allowed based on a whitelist and a blacklist of substrings.

    Args:
        s (str): The input string to be checked.
        whitelist (list of str): A list of substrings that are allowed.
        blacklist (list of str): A list of substrings that are not allowed.

    Returns:
        tuple of bool, bool: A tuple of booleans indicating whether the string is allowed by the whitelist and blacklist, respectively.
    """
    if exact_match:
        white_list_allowed = (len(whitelist) == 0 or any([w == s for w in whitelist]))
        black_list_allowed = (len(blacklist) == 0 or all([b != s for b in blacklist]))
    else:
        white_list_allowed = (len(whitelist) == 0 or any([w.lower() in s.lower() for w in whitelist]))
        black_list_allowed = (len(blacklist) == 0 or all([b.lower() not in s.lower() for b in blacklist]))
    return (white_list_allowed, black_list_allowed)


def cleanup_documents(dry_run=DRY_RUN,
                       archive_docs=ARCHIVE_DOCS,
                       supervise_run=SUPERVISE_RUN,
                       confirm_before_modifying=False,
                       verbose=VERBOSE,
                       item_whitelist=[],
                       item_blacklist=[],
                       doc_whitelist=[],
                       doc_blacklist=[],
                       tag_whitelist=[],
                       tag_blacklist=[],
                       op_cli_path="",
                       generate_share_links=False,
                       reattached_tag=""):
    if op_cli_path != "" and os.path.exists(op_cli_path):
        global OP_CLI_PATH
        OP_CLI_PATH = op_cli_path
    verbose |= supervise_run # if we're supervising, we're verbose
    generate_share_links |= supervise_run # if we're supervising, we're generating share links
    confirm_before_modifying |= supervise_run # if we're supervising, we're confirming before modifying
    archive_str = "--archive" if archive_docs else ""
    dry_run_str = "--dry-run" if dry_run else ""
    reattached_tag = reattached_tag.replace('"', '').replace("'", "").strip()
    
    # get all items from 1password
    all_docs = [i for i in J(f"item list") if i["category"] == "DOCUMENT"]
    tag_str = (" --tags " + ','.join([f'"{t}"' for t in tag_whitelist])) if len(tag_whitelist) > 0 else ""
    all_itms_w_archive = [i for i in J(f"item list --include-archive" + tag_str) if i["category"] != "DOCUMENT"]
    
    
    # Keep track of reattached, skipped, and failed documents
    # for reporting at the end.
    reattached_docs = defaultdict(list)
    skipped_docs = defaultdict(list)
    removed_docs = defaultdict(list)
    removal_pending_docs = defaultdict(list)
    failed_docs = defaultdict(list)
    
    skipped_itms = set()
    item_whitelist += doc_whitelist
    item_blacklist += doc_blacklist
    for doc in all_docs:
        if (wbla := allowed_by_white_black_lists(doc["title"], item_whitelist, item_blacklist)) and False in wbla:
            rs = "item blacklisted" if not wbla[1] else "item not on whitelist"
            skipped_docs[rs].append({"title": doc["title"]})
            skipped_itms.add(doc["id"])
        if doc["id"] not in skipped_itms:
            for tag in doc.get("tags", []):
                if (wbla := allowed_by_white_black_lists(tag, tag_whitelist, tag_blacklist, exact_match=True)) and False in wbla:
                    rs = "item tag blacklisted" if not wbla[1] else "item tag not on whitelist"
                    skipped_docs[rs].append({"title": doc["title"]})
                    skipped_itms.add(doc["id"])
                    break
    
    all_docs = [i for i in all_docs if i["id"] not in skipped_itms]
    removed_doc_ids = set()
    
    # main loop over the documents.
    # perform a set of checks on each to determine if it should be
    # removed, reattached, or skipped.
    for doc in tqdm(all_docs, desc=f"(Step 1 of 3; no changes being made) Checking {len(all_docs)} documents for potential cleanup"):
        try:
            doc_i = doc["id"]
            doc_j = J("item get " + doc_i)
        except subprocess.CalledProcessError as e:
            doc = next(i for i in all_docs if i["id"] == doc_i)
            if verbose: print(f"-- Skipping: {doc['title']}, failed to get item: {e}")
            failed_docs[f"failed to get doc"].append({"item": doc["title"], "document": doc["title"], "error": e})
            continue
        doc_name = doc_j["title"]
        if verbose: print(f"Processing: {doc_name} ({dry_run=})")
        doc_files = [i for i in doc_j.get("files", []) if i["id"] != ""]
        
        # if no files, remove the document
        if len(doc_files) == 0:
            if verbose: print(f"-- Removing: {doc_name}, no files")
            removed_docs["no files"].append(doc_j)
            removed_doc_ids.add(doc_i)
            continue
        
        doc_size = doc_files[0]["size"]
        
        # if referenced by an archived item, remove the document.
        # only really care about documents made in the upgrade process,
        # and it's expensive to "get" many items, so only consider a document
        # if it has a name that looks like it was created during the upgrade
        # process (i.e. it has a name like "Document - Item Name"). 
        # Then, we can quickly find candidate items by name.
        doc_name_split = doc_name.split(" - ")
        # if the string after the " - " ends in a file extension, that doesn't
        # match. If the string after the " - " doens't end in a file extension,
        # then it's a match.
        if len(doc_name_split) < 2 or ("." in doc_name_split[-1] and len(doc_name_split[-1].split(".")[-1]) > 0 and len(doc_name_split[-1].split(".")[-1]) < 5):
            if verbose: print(f"-- Skipping: {doc_name}, not named like document from 1P v7 upgrade")
            skipped_docs["not named like document from 1P v7 upgrade"].append(doc_j)
            continue
        
        itm_check_name = doc_name_split[-1].strip()
        matching_itms = [i for i in all_itms_w_archive if i["title"].strip() == itm_check_name]
        # first check for explicit references to the document in archived items
        for candidate_itm in matching_itms:
            if candidate_itm.get("state", "") != "ARCHIVED": continue
            try:
                itm_j = J("item get " + candidate_itm["id"])
            except subprocess.CalledProcessError as e:
                if verbose: print(f"-- Skipping: {candidate_itm['title']}, failed to get item: {e}")
                failed_docs[f"failed to get item"].append({"item": candidate_itm["title"], "document": doc_name, "error": e})
                continue
            # check references and see if the current document is referenced
            refs = [r for r in itm_j.get("fields",[]) if r.get("type", "") == "REFERENCE" and r.get("value", "") == doc_i]
            if len(refs) == 0: continue
            # remove doc
            if verbose: print(f"-- Removing: {doc_name}, referenced by archived item: {candidate_itm['title']}")
            removed_docs["referenced by archived item"].append(doc_j | {"referenced by": candidate_itm})
            removed_doc_ids.add(doc_i)
            break
        
        if doc_i in removed_doc_ids: continue
        
        # Check if document is name-matched to a non-archive item that
        # does not already contain the document as an attachment.
        # If not, reattach, otherwise remove.
        for candidate_itm in matching_itms:
            if candidate_itm.get("state", "") == "ARCHIVED": continue
            try:
                itm_j = J("item get " + candidate_itm["id"])
            except subprocess.CalledProcessError as e:
                if verbose: print(f"-- Skipping: {candidate_itm['title']}, failed to get item: {e}")
                failed_docs[f"failed to get item"].append({"item": candidate_itm["title"], "document": doc_name, "error": e})
                continue
            itm_files = itm_j.get("files", [])
            if len(itm_files) == 0:
                continue
            if doc_size in [f["size"] for f in itm_files]:
                # remove
                if verbose: print(f"-- Removing: {doc_name}, already attached to item (size match): {candidate_itm['title']}")
                removed_docs["already attached to item (size match)"].append(doc_j | {"referenced by": candidate_itm})
                removed_doc_ids.add(doc_i)
                break
            if doc_name.replace(f" - {candidate_itm['title']}", "") in [f["name"] for f in itm_files]:
                # remove
                if verbose: print(f"-- Removing: {doc_name}, already attached to item (name match): {candidate_itm['title']}")
                removed_docs["already attached to item (name match)"].append(doc_j | {"referenced by": candidate_itm})
                removed_doc_ids.add(doc_i)
                break
            # reattach doc
            if verbose: print(f"-- Reattaching: {doc_name}, fuzzy referenced by item: {candidate_itm['title']}")
            reattached_docs[doc_i].append(doc_j | {"referenced by": candidate_itm})
            # only do one fuzzy reattachment per document
            break
        
        if doc_i in removed_doc_ids: continue
        
        # Nothing matched, but it had a name that suggests it was made as
        # part of the 1P v7 upgrade process, so add it to the remove docs
        # list with an item to check with user.
        if verbose: print(f"-- Remove pending approval: {doc_name}, no matching items")
        if confirm_before_modifying:
            removal_pending_docs["no matching items"].append(doc_j)
        else:
            # skip
            if verbose: print(f"-- Skipping: {doc_name}, no matching items")
            skipped_docs["no matching items"].append(doc_j)
    
    # if confirmation was selected, print summary of pending changes.
    # first confirm the reattachments and removals (not pending).
    # then confirm the pending removals.
    num_reattached_total = sum([len(v) for v in reattached_docs.values()])
    num_removed_non_pending_total = sum([len(v) for v in removed_docs.values()])
    num_removed_pending_total = sum([len(v) for v in removal_pending_docs.values()])
    if confirm_before_modifying:
        print(f"Found {num_reattached_total} document{'' if num_reattached_total == 1 else 's'} to reattach.")
        # list the items that will be reattached
        if num_reattached_total > 0:
            print("Reattaching:")
            for doc_id, reattachments in reattached_docs.items():
                for reattachment in reattachments:
                    print(f"  '{reattachment['title']}' to '{reattachment['referenced by']['title']}'")
        print(f"Found {num_removed_non_pending_total} document{'' if num_removed_non_pending_total == 1 else 's'} to remove.")
        # list the items that will be removed
        if num_removed_non_pending_total > 0:
            print("Removing:")
            for reason, docs in removed_docs.items():
                print(f"  because {reason}: {len(docs)}")
                for doc in docs:
                    if 'referenced by' not in doc:
                        print(f"    '{doc['title']}'")
                    else:
                        print(f"    '{doc['title']}' referenced by '{doc['referenced by']['title']}'")
        if num_removed_non_pending_total > 0 or num_reattached_total > 0:
            print("Shall I continue and reattach and/or remove all documents found? (Y/n)")
            rsp = input()
            if rsp.lower().strip() == "n":
                print("Cancelling. No changes made.")
                return
        print(f"Found {num_removed_pending_total} document{'' if num_removed_pending_total == 1 else 's'} to remove pending approval.")
        # list the items that will be removed pending approval
    if num_removed_pending_total > 0:
        print("Remove pending approval:")
        for reason, docs in removal_pending_docs.items():
            print(f"  because {reason}: {len(docs)}")
            for doc in docs:
                print(f"    {doc['title']}")
        print("Shall I remove the pending documents also? (y/N)")
        rsp = input()
        if rsp.lower().strip() == "y":
            removed_docs |= removal_pending_docs
            num_removed_non_pending_total += num_removed_pending_total
    
    # reattach docs
    if dry_run: print("DRY RUN: No changes will be made.")
    for doc_id, itm_dicts in tqdm(reattached_docs.items(), desc=f"(Step 2 of 3) Reattaching {num_reattached_total} documents"):
        for itm_dict in itm_dicts:
            itm_i = itm_dict["referenced by"]["id"]
            itm_vid = itm_dict["referenced by"]["vault"]["id"]
            itm_name = itm_dict["referenced by"]["title"]
            doc_name = itm_dict["title"]
            doc_name = sanitize(doc_name.replace(f" - {itm_name}", ""))
            doc_name_escaped = doc_name.replace(".", "\\.").replace('"', '').replace("'", "")
            if verbose: print(f"-- Reattaching '{doc_name}' to '{itm_name}'")
            with tempfile.TemporaryDirectory() as tmp_dir:
                try:
                    out_file = os.path.join(tmp_dir, doc_name.replace(" ", "_").replace('"', '').replace("'", ""))
                    if verbose: print(f"---- copying file to temp dir: {out_file}")
                    R(f"document get {doc_id} --vault {itm_vid} --out-file '{out_file}'")
                    
                    if verbose: print(f"---- attaching file to item")
                    R(f"item edit {itm_i} --vault {itm_vid} {dry_run_str} '{doc_name_escaped}[file]={out_file}'")
                except (subprocess.CalledProcessError, KeyError) as e:
                    if verbose: print(f"---- Skipping: {doc_name} to {itm_name}, failed to reattach document: {e}")
                    failed_docs[f"failed to reattach document"].append({"item": itm_name, "document": doc_name, "error": e})
                try:
                    if reattached_tag != "":
                        if verbose: print(f"---- adding reattached tag to item")
                        itm_tags = itm_dict["referenced by"].get("tags", [])
                        if reattached_tag + " fuzzy" not in itm_tags:
                            itm_tags.append(reattached_tag + " fuzzy")
                            itm_tags = ','.join([f'"{t}"' for t in itm_tags])
                            R(f"item edit {itm_i} --vault {itm_vid} {dry_run_str} --tags {itm_tags}")
                except (subprocess.CalledProcessError, KeyError) as e:
                    if verbose: print(f"---- Skipping: {doc_name} to {itm_name}, failed to add reattached tag to item: {e}")
                    failed_docs[f"failed to add reattached tag to item"].append({"item": itm_name, "document": doc_name, "error": e})
                try:
                    # tag doc before deleting
                    if verbose: print(f"---- tagging document before deleting")
                    if reattached_tag + " deleted" not in itm_dict.get("tags", []):
                        doc_tags = itm_dict.get("tags", [])
                        doc_tags.append(reattached_tag + " deleted")
                        doc_tags = ','.join([f'"{t}"' for t in doc_tags])
                        doc_vid = itm_dict["vault"]["id"]
                        R(f"item edit {doc_id} --vault {doc_vid} {dry_run_str} --tags {doc_tags}")
                except (subprocess.CalledProcessError, KeyError) as e:
                    if verbose: print(f"---- Skipping: {doc_name} to {itm_name}, failed to tag document: {e}")
                    failed_docs[f"failed to tag document before removal"].append({"item": itm_name, "document": doc_name, "error": e})
                try:
                    if verbose: print(f"---- deleting document")
                    if not dry_run:
                        R(f"item delete {doc_id} --vault {doc_vid} {archive_str}")
                except (subprocess.CalledProcessError, KeyError) as e:
                    if verbose: print(f"---- Skipping: {doc_name} to {itm_name}, failed to delete document: {e}")
                    failed_docs[f"failed to delete document"].append({"item": itm_name, "document": doc_name, "error": e})

    # remove docs
    if dry_run: print("DRY RUN: No changes will be made.")
    removed_docs_list = [doc for docs in removed_docs.values() for doc in docs]
    for itm_dict in tqdm(removed_docs_list, desc=f"(Step 3 of 3) Removing {num_removed_non_pending_total} documents"):
        # tag doc before deleting
        doc_id = itm_dict["id"]
        doc_vid = itm_dict["vault"]["id"]
        doc_name = itm_dict["title"]
        if verbose: print(f"---- tagging document before deleting")
        try:
            if reattached_tag + " deleted" not in itm_dict.get("tags", []):
                doc_tags = itm_dict.get("tags", [])
                doc_tags.append(reattached_tag + " deleted")
                doc_tags = ','.join([f'"{t}"' for t in doc_tags])
                R(f"item edit {doc_id} --vault {doc_vid} {dry_run_str} --tags {doc_tags}")
        except subprocess.CalledProcessError as e:
            if verbose: print(f"---- Skipping: {doc_name}, failed to remove document: {e}")
            failed_docs[f"failed to tag document before removal"].append({"item": doc_name, "document": doc_name, "error": e})
        if verbose: print(f"-- Removing '{doc_name}'")
        try:
            if not dry_run:
                R(f"item delete {doc_id} --vault {doc_vid} {archive_str}")
        except subprocess.CalledProcessError as e:
            if verbose: print(f"---- Skipping: {doc_name}, failed to remove document: {e}")
            failed_docs[f"failed to delete document"].append({"item": doc_name, "document": doc_name, "error": e})

    # print report, starting with summary, then if verbose list the details
    # of the reattached, removed, skipped, and failed documents.
    if dry_run: print("DRY RUN: No changes were made.")
    reattached_item_names = list(set([doc["referenced by"]["title"] for docs in reattached_docs.values() for doc in docs]))
    num_skipped = sum([len(v) for v in skipped_docs.values()])
    num_failed = sum([len(v) for v in failed_docs.values()])
    print(f"Reattached {len(reattached_docs)} documents to {len(reattached_item_names)} items.")
    if verbose and len(reattached_docs) > 0:
        print("Reattached documents:")
        for doc_id, reattachments in reattached_docs.items():
            for reattachment in reattachments:
                print(f"  {reattachment['title']} to {reattachment['referenced by']['title']}")
    print(f"Removed {len(removed_docs)} documents.")
    if verbose and num_skipped > 0:
        print(f"Skipped {num_skipped} documents because")
        for k,v in skipped_docs.items():
            print(f"  {k}: {len(v)}")
    if verbose and len(failed_docs) > 0:
        print(f"Failed to reattach {num_failed} documents because")
        for k,v in failed_docs.items():
            print(f"  {k}: {len(v)}")
    
    # Print a full report to a csv file, 1password_document_reattacher_report_{current_date_time}.csv in the current directory
    current_date_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    csv_file_name = f"1password_document_reattacher_report_{current_date_time}.csv"
    with open(csv_file_name, "w") as f:
        writer = csv.writer(f)
        writer.writerow(["document", "action", "item", "reason"])
        for doc_id, reattachments in reattached_docs.items():
            for reattachment in reattachments:
                writer.writerow([reattachment["title"], "reattached", reattachment["referenced by"]["title"], "matched by item/doc name"])
        for reason, docs in removed_docs.items():
            for doc in docs:
                writer.writerow([doc["title"], "removed", doc["referenced by"]["title"] if "referenced by" in doc else "", reason])
        for reason, docs in skipped_docs.items():
            for doc in docs:
                writer.writerow([doc["title"], "skipped", "", reason])
        for reason, docs in failed_docs.items():
            for doc in docs:
                writer.writerow([doc["item"], reason, doc["document"], doc["error"]])
    
    print(f"Done. Report written to {os.path.join(os.getcwd(), csv_file_name)}")
            
    return

def main(dry_run=DRY_RUN,
        archive_docs=ARCHIVE_DOCS,
        supervise_run=SUPERVISE_RUN,
        confirm_before_modifying=False,
        verbose=VERBOSE,
        item_whitelist=[],
        item_blacklist=[],
        doc_whitelist=[],
        doc_blacklist=[],
        tag_whitelist=[],
        tag_blacklist=[],
        op_cli_path="",
        generate_share_links=False,
        reattached_tag=""):
    verbose |= supervise_run # if we're supervising, we're verbose
    generate_share_links |= supervise_run # if we're supervising, we're generating share links
    confirm_before_modifying |= supervise_run # if we're supervising, we're confirming before reattaching
    archive_str = "--archive" if archive_docs else ""
    dry_run_str = "--dry-run" if dry_run else ""
    reattached_tag = reattached_tag.replace('"', '').replace("'", "").strip()
    
    # print opening and list user options
    if verbose:
        print("1Password document reattacher running with options:")
        print("\n".join([f"{dry_run=}", f"{archive_docs=}", f"{supervise_run=}", f"{verbose=}", f"{item_whitelist=}", f"{item_blacklist=}", f"{doc_whitelist=}", f"{doc_blacklist=}", f"{tag_whitelist=}", f"{tag_blacklist=}", f"{op_cli_path=}", f"{generate_share_links=}"]))
    
    # get all items from 1password
    all_itms = J(f"item list")

    if verbose:
        # Print some fun information about the items in the vault,
        # just for fun.
        itms_by_category = defaultdict(list)
        itms_by_tag = defaultdict(list)
        itms_by_vault = defaultdict(list)
        for itm in all_itms:
            itms_by_category[itm["category"]].append(itm)
            itms_by_vault[itm["vault"]["name"]].append(itm)
            for tag in itm.get("tags", []):
                itms_by_tag[tag].append(itm)

        # Print total number of items
        print(f"Total number of items: {len(all_itms)}")
        # Print number of items by vault, in descending order
        # by number of items, and print the percentage of the total
        # for each vault.
        print("\nVaults:")
        itms = itms_by_vault
        sorted_names = sorted(itms.keys(), key=lambda k: len(itms[k]), reverse=True)
        longest_name_len = max([len(n) for n in sorted_names])
        for k in sorted_names:
            v = itms[k]
            print(f"  {k:{longest_name_len}}  {len(v)} ({len(v)/len(all_itms)*100:.1f}%)")

        # Again for categories
        print("\nCategories:")
        itms = itms_by_category
        sorted_names = sorted(itms.keys(), key=lambda k: len(itms[k]), reverse=True)
        longest_name_len = max([len(n) for n in sorted_names])
        for k in sorted_names:
            v = itms[k]
            print(f"  {k:{longest_name_len}}  {len(v)} ({len(v)/len(all_itms)*100:.1f}%)")
            
        # Again for tags
        print("\nTags:")
        itms = itms_by_tag
        sorted_names = sorted(itms.keys(), key=lambda k: len(itms[k]), reverse=True)
        longest_name_len = max([len(n) for n in sorted_names])
        for k in sorted_names:
            v = itms[k]
            print(f"  {k:{longest_name_len}}  {len(v)} ({len(v)/len(all_itms)*100:.1f}%)")

    # Keep track of reattached, skipped, and failed documents
    # for reporting at the end.
    reattached_docs = defaultdict(list)
    skipped_docs = defaultdict(list)
    failed_docs = defaultdict(list)

    # precheck items skipped by blacklist or whitelist
    all_itms = [i for i in all_itms if i["category"] != "DOCUMENT"]
    skipped_itms = set()
    for itm in all_itms:
        if (wbla := allowed_by_white_black_lists(itm["title"], item_whitelist, item_blacklist)) and False in wbla:
            rs = "item blacklisted" if not wbla[1] else "item not on whitelist"
            skipped_docs[rs].append({"item": itm["title"], "document": "", "item link": ""})
            skipped_itms.add(itm["id"])
        if itm["id"] not in skipped_itms:
            for tag in itm.get("tags", []):
                if (wbla := allowed_by_white_black_lists(tag, tag_whitelist, tag_blacklist, exact_match=True)) and False in wbla:
                    rs = "item tag blacklisted" if not wbla[1] else "item tag not on whitelist"
                    skipped_docs[rs].append({"item": itm["title"], "document": "", "item link": ""})
                    skipped_itms.add(itm["id"])
                    break

    # Get item ids for all items that are not skipped
    itm_ids = [i["id"] for i in all_itms if i["id"] not in skipped_itms]

    if dry_run: print("DRY RUN: No changes will be made.")

    # Loop over each item, check for references to documents,
    # reattach the documents, and delete the document references
    # and document items if successful.
    count = 0
    max_count = 0
    for itm_i in tqdm(itm_ids, desc=f"(Step 1 of 2; no changes being made) Checking {len(itm_ids)} items for reattachable documents"):
        count += 1
        if count > max_count and max_count > 0:
            break
        try:
            itm_j = J("item get " + itm_i)
        except subprocess.CalledProcessError as e:
            itm = next(i for i in all_itms if i["id"] == itm_i)
            if verbose: print(f"-- Skipping: {itm['title']}, failed to get item: {e}")
            failed_docs[f"failed to get item"].append({"item": itm['title'], "document": "", "item link": "", "error": e})
            continue
        itm_name = itm_j["title"]
        itm_vid = itm_j["vault"]["id"]
        # Get item fields that are references to other items
        refs = [r for r in itm_j.get("fields",[]) if r.get("type", "") == "REFERENCE"]
        try:
            itm_lnk = S(f"item get {itm_i} --share-link --vault {itm_vid}") if generate_share_links else ""
        except subprocess.CalledProcessError as e:
            if verbose: print(f"-- Skipping: {itm['title']}, , failed to get item link: {e}")
            failed_docs[f"failed to get item link"].append({"item": itm_name, "document": "", "item link": "", "error": e})
            continue
        if verbose and len(refs) > 0:
            print(f"Processing: {itm_name} ({dry_run=})")
            print(f"-- {itm_lnk}")
            print(f"-- Found {len(refs)} references")
        # Loop over each reference to a document
        for ref in refs:
            try:
                ref_id = ref["value"]
                # print(ref)
                ref_j = J(f"item get {ref_id}")
                ref_vid = ref_j["vault"]["id"]
                ref_name = ref_j["title"]
                # Check if the document is allowed by the whitelist and blacklist
                if (wbla := allowed_by_white_black_lists(ref_name, doc_whitelist, doc_blacklist)) and False in wbla:
                    rs = "doc blacklisted" if not wbla[1] else "doc not on whitelist"
                    if verbose: print(f"-- Skipping: {ref_name}, {rs}")
                    skipped_docs[rs].append({"item": itm_name, "document": ref_name, "item link": itm_lnk})
                    continue
                if ref_j["category"] != "DOCUMENT":
                    if verbose: print(f"-- Skipping: {ref_name}, not a document")
                    skipped_docs["not a document"].append({"item": itm_name, "document": ref_name, "item link": itm_lnk})
                    continue
                
                # prepare for copying document file to temp dir, and get
                # permission to continue if supervising
                ref_name = sanitize(ref_name.replace(f" - {itm_name}", ""))
                ref_sec = ref["section"]["label"]
                ref_field_id = ref["id"]
                if verbose:
                    ref_lnk = S(f"item get {ref_id} --share-link --vault {ref_vid}") if generate_share_links else ""
                    print(f"-- Processing: {ref_name}")
                    print(f"---- {ref_lnk}")
                    if supervise_run:
                        print(f"---- Shall I continue and reattach this document? (Y/n)")
                        rsp = input()
                        if rsp.lower().strip() == "n":
                            print(f"---- User skipping: {ref_name}")
                            skipped_docs["user skipped"].append({"item": itm_name, "document": ref_name, "item link": itm_lnk})
                            continue
                
                if len(ref_j["files"]) > 1:
                    if verbose: print(f"---- Skipping: {ref_name}, more than one file")
                    skipped_docs["more than one file"].append({"item": itm_name, "document": ref_name, "item link": itm_lnk})
                
                ref_file_name = ref_j["files"][0]["name"]
                ref_name_escaped = ref_file_name.replace(".", "\\.").replace('"', '').replace("'", "")
                reattached_docs[ref_id].append({
                    "item": itm_name, 
                    "document": ref_name, 
                    "item link": itm_lnk,
                    "ref vid": ref_vid,
                    "ref name escaped": ref_name_escaped,
                    "ref sec": ref_sec,
                    "ref field id": ref_field_id,
                    "ref file name": ref_file_name,
                    "item id": itm_i,
                    "item vid": itm_vid,
                    "item tags": itm_j.get("tags", []),
                    "doc tags": ref_j.get("tags", []),
                    })
                if verbose: print(f"---- Will reattach: {ref_name}")
            except (subprocess.CalledProcessError, KeyError) as e:
                if verbose: print(f"---- Skipping: {ref_name}, failed to check document: {e}")
                failed_docs[f"failed to check document"].append({"item": itm_name, "document": ref_name, "item link": itm_lnk, "error": e})
                continue
            
    # Loop over each document that was found and reattach it
    num_reattached_total = sum([len(v) for v in reattached_docs.values()])
    
    if num_reattached_total == 0:
        print("No documents to reattach.")
        return
    
    if dry_run: print("DRY RUN: No changes will be made.")
    
    if confirm_before_modifying:
        # print a summary of the number of documents to be reattached and then
        # ask the user if they want to reattach all documents.
        print(f"Found {num_reattached_total} document{'' if num_reattached_total == 1 else 's'} to reattach.")
        # print details of the reattachments
        print("Reattaching:")
        for ref_id, itm_dicts in reattached_docs.items():
            for itm_dict in itm_dicts:
                print(f"  '{itm_dict['document']}' to '{itm_dict['item']}'")
        print("Shall I continue and reattach all documents? (Y/n)")
        rsp = input()
        if rsp.lower().strip() == "n":
            print("Cancelling. No changes made.")
            return
        
    for ref_id, itm_dicts in tqdm(reattached_docs.items(), desc=f"(Step 2 of 2) Reattaching {num_reattached_total} documents"):
        for itm_dict in itm_dicts:
            ref_vid = itm_dict["ref vid"]
            ref_name_escaped = itm_dict["ref name escaped"]
            ref_sec = itm_dict["ref sec"]
            ref_field_id = itm_dict["ref field id"]
            itm_i = itm_dict["item id"]
            itm_vid = itm_dict["item vid"]
            itm_name = itm_dict["item"]
            ref_name = itm_dict["document"]
            ref_file_name = itm_dict["ref file name"]
            itm_lnk = itm_dict["item link"]
            itm_tags = itm_dict["item tags"]
            doc_tags = itm_dict["doc tags"]
            if verbose: print(f"-- Reattaching '{ref_name}' to '{itm_name}'")
            try:
                with tempfile.TemporaryDirectory() as tmp_dir:
                    out_file = os.path.join(tmp_dir, ref_file_name.replace(" ", "_").replace('"', '').replace("'", ""))
                    if verbose: print(f"---- copying file to temp dir: {out_file}")
                    R(f"document get {ref_id} --vault {ref_vid} --out-file '{out_file}'")
                    
                    if verbose: print(f"---- attaching file to item")
                    R(f"item edit {itm_i} --vault {itm_vid} {dry_run_str} '{ref_name_escaped}[file]={out_file}'")
                    
                    if reattached_tag != "":
                        if verbose: print(f"---- adding reattached tag to item")
                        if reattached_tag not in itm_tags:
                            itm_tags.append(reattached_tag)
                            itm_tags = ','.join([f'"{t}"' for t in itm_tags])
                            R(f"item edit {itm_i} --vault {itm_vid} {dry_run_str} --tags {itm_tags}")
                    
                    if verbose: print(f"---- deleting document reference")
                    R(f"item edit {itm_i} --vault {itm_vid} {dry_run_str} '{ref_sec}.{ref_field_id}[delete]'")
                    
                    if verbose: print(f"---- tagging document before deletion")
                    if reattached_tag + " deleted" not in doc_tags:
                        doc_tags.append(reattached_tag + " deleted")
                        doc_tags = ','.join([f'"{t}"' for t in doc_tags])
                        R(f"item edit {ref_id} --vault {ref_vid} {dry_run_str} --tags {doc_tags}")
                    
                    if verbose: print(f"---- deleting document")
                    if not dry_run:
                        R(f"item delete {ref_id} --vault {ref_vid} {archive_str}")
            except (subprocess.CalledProcessError, KeyError) as e:
                if verbose: print(f"---- Skipping: {ref_name} to {itm_name}, failed to reattach document: {e}")
                failed_docs[f"failed to reattach document"].append({"item": itm_name, "document": ref_name, "item link": itm_lnk, "error": e})
    # make reattached_docs a list of dicts instead of a dict of lists of dicts
    reattached_docs = [doc for docs in reattached_docs.values() for doc in docs]
    
    # Print report of reattached, skipped, and failed documents.
    # First print a summary of the number of each type of document.
    # For skipped and failed documents, we'll print the number for each
    # reason (dict key).

    if dry_run: print("DRY RUN: No changes were made.")
    reattached_item_names = list(set([doc["item"] for doc in reattached_docs]))
    num_skipped = sum([len(v) for v in skipped_docs.values()])
    num_failed = sum([len(v) for v in failed_docs.values()])
    print(f"Reattached {len(reattached_docs)} documents to {len(reattached_item_names)} items.")
    if num_skipped > 0:
        print(f"Skipped {num_skipped} documents because")
        for k,v in skipped_docs.items():
            print(f"  {k}: {len(v)}")
    if len(failed_docs) > 0:
        print(f"Failed to reattach {num_failed} documents because")
        for k,v in failed_docs.items():
            print(f"  {k}: {len(v)}")

    # Print the details for skipped and failed documents.
    if verbose and len(skipped_docs) > 0:
        print("")
        print("Skipped documents:")
        for k,v in skipped_docs.items():
            print(f"  Reason: {k}")
            doc_list_str = ', '.join([f"{doc['item']}" + (f" - {doc['document']}" if doc['document'] != "" else "") for doc in v])
            print(f"    {doc_list_str}")
    if verbose and len(failed_docs) > 0:
        print("")
        print("Failed documents:")
        for k,v in failed_docs.items():
            print(f"  Problem: {k}")
            doc_list_str = ', '.join([f"{doc['item']}" + (f" - {doc['document']}" if doc['document'] != "" else "") for doc in v])
            print(f"    {doc_list_str}")
            
    # Print a full report to a csv file, 1password_document_reattacher_report_{current_date_time}.csv in the current directory
    current_date_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    with open(f"1password_document_reattacher_report_{current_date_time}.csv", "w") as f:
        writer = csv.writer(f)
        writer.writerow(["item", "document", "item link", "status"])
        for doc in reattached_docs:
            writer.writerow([doc["item"], doc["document"], doc["item link"], "reattached"])
        for k,v in skipped_docs.items():
            for doc in v:
                writer.writerow([doc["item"], doc["document"], doc["item link"], f"skipped: {k}"])
        for k,v in failed_docs.items():
            for doc in v:
                writer.writerow([doc["item"], doc["document"], doc["item link"], f"error: {doc['error']}"])    
    
    print(f"Done. Report written to {os.path.join(os.getcwd(), '1password_document_reattacher.csv')}")

if __name__ == "__main__":
    # define command line arguments
    parser = argparse.ArgumentParser(description="""1Password document reattacher: Reattach 1Password documents to items that were automatically converted to standalone documents when the user upgraded to 1Password 7. 

This script is used to convert documents that were created automatically from item attachments during the upgrade process to 1Password v7 back into attachments. With the release of 1Password 8, attachments are back, and this script reverses the process that took place during the upgrade to version 7, replacing document references with attachments and removing the standalone document items and document references.

This will replace all references to stand-alone documents with attachments, and then delete the stand-alone documents and document references. It will also archive the original documents if the `--archive-docs` flag is set. This is a destructive process and cannot be easily undone, so be sure to **make a backup of your 1Password account using *File -> Export -> <your account name>* before running this script**, and use the `--dry-run` or `--supervise-run` options for finer visibility and control over the process.

There are two 

A report of the changes will be saved to a csv file in the current directory.""")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually make any changes, just print what would be done.")
    parser.add_argument("--delete-docs", action="store_true", help="Delete documents after they're successfully reattached instead of archiving them.")
    parser.add_argument("--supervise", action="store_true", help="Ask the user whether to reattach each document.")
    parser.add_argument("--confirm-before-modifying", action="store_true", help="Ask the user before starting to reattach/modify documents.")
    parser.add_argument("--verbose", action="store_true", help="Print more information about the process.")
    parser.add_argument("--item-whitelist", nargs="*", help="One or more quoted strings, one of which must be present in an item's title for the item to be processed.")
    parser.add_argument("--item-blacklist", nargs="*", help="One or more quoted strings, none of which may be present in an item's title for the item to be processed.")
    parser.add_argument("--doc-whitelist", nargs="*", help="One or more quoted strings, one of which must be present in a document's title for the document to be processed.")
    parser.add_argument("--doc-blacklist", nargs="*", help="One or more quoted strings, none of which may be present in a document's title for the document to be processed.")
    parser.add_argument("--tag-whitelist", nargs="*", help="One or more quoted strings, one of which must match an item's tags for the item to be processed.")
    parser.add_argument("--tag-blacklist", nargs="*", help="One or more quoted strings, none of which may match an item's tags for the item to be processed.")
    parser.add_argument("--op-cli-path", help="The path to the op command line tool.", default=OP_CLI_PATH)
    parser.add_argument("--generate-share-links", action="store_true", help="Generate share links for items and documents to aid in checking/supervising and that appear in the report.")
    parser.add_argument("--reattach-tag", help="The tag to add to items that have documents reattached.", default="linked docs reattached")
    parser.add_argument("--cleanup-documents", action="store_true", help="Instead of reattaching documents based on explicit links between items, clean up standalone (single-file) document items that were not reattached when running the script normally without this option. Cleans us documents whose linking items are missing/archived/deleted, whose files are missing, or that appear to have been created during the 1P v7 upgrade base on their name.")
    
    # parse command line arguments
    args = parser.parse_args()

    if args.cleanup_documents:
        cleanup_documents(dry_run=args.dry_run,
            archive_docs=not args.delete_docs,
            supervise_run=args.supervise,
            confirm_before_modifying=args.confirm_before_modifying,
            verbose=args.verbose,
            item_whitelist=args.item_whitelist if args.item_whitelist is not None else [],
            item_blacklist=args.item_blacklist if args.item_blacklist is not None else [],
            doc_whitelist=args.doc_whitelist if args.doc_whitelist is not None else [],
            doc_blacklist=args.doc_blacklist if args.doc_blacklist is not None else [],
            tag_whitelist=args.tag_whitelist if args.tag_whitelist is not None else [],
            tag_blacklist=args.tag_blacklist if args.tag_blacklist is not None else [],
            op_cli_path=args.op_cli_path,
            generate_share_links=args.generate_share_links,
            reattached_tag=args.reattach_tag)
    else:
        main(dry_run=args.dry_run, 
            archive_docs=not args.delete_docs, 
            supervise_run=args.supervise,
            confirm_before_modifying=args.confirm_before_modifying,
            verbose=args.verbose, 
            item_whitelist=args.item_whitelist if args.item_whitelist is not None else [],
            item_blacklist=args.item_blacklist if args.item_blacklist is not None else [],
            doc_whitelist=args.doc_whitelist if args.doc_whitelist is not None else [],
            doc_blacklist=args.doc_blacklist if args.doc_blacklist is not None else [],
            tag_whitelist=args.tag_whitelist if args.tag_whitelist is not None else [],
            tag_blacklist=args.tag_blacklist if args.tag_blacklist is not None else [],
            op_cli_path=args.op_cli_path,
            generate_share_links=args.generate_share_links,
            reattached_tag=args.reattach_tag)
        
        # ask user if they want to followup with cleanup_documents
        print("Would you like to follow up with additional cleaning up of 1P v7 documents? (Y/n)")
        rsp = input()
        if rsp.lower().strip() != "n":
            cleanup_documents(dry_run=args.dry_run,
                archive_docs=not args.delete_docs,
                supervise_run=args.supervise,
                confirm_before_modifying=args.confirm_before_modifying,
                verbose=args.verbose,
                item_whitelist=args.item_whitelist if args.item_whitelist is not None else [],
                item_blacklist=args.item_blacklist if args.item_blacklist is not None else [],
                doc_whitelist=args.doc_whitelist if args.doc_whitelist is not None else [],
                doc_blacklist=args.doc_blacklist if args.doc_blacklist is not None else [],
                tag_whitelist=args.tag_whitelist if args.tag_whitelist is not None else [],
                tag_blacklist=args.tag_blacklist if args.tag_blacklist is not None else [],
                op_cli_path=args.op_cli_path,
                generate_share_links=args.generate_share_links,
                reattached_tag=args.reattach_tag)
        