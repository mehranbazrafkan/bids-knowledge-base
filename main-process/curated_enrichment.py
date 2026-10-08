#!/usr/bin/env python3
"""Authored explanations for BIDS validation issue codes.

The schema says what a check tests. It does not say why the check exists, what
usually causes it to fail, or what a user should do next, and those are the three
things somebody staring at a validator error actually needs. This module supplies
them, keyed by the issue code the validator prints.

Scope and discipline
--------------------
Everything here explains the BIDS standard itself. Nothing here describes a
particular tool's user interface, because the same explanation has to serve a
user working from a command line, a graphical converter or a notebook.

The rules the content obeys, in order of importance:

1. Never invent a requirement. If the standard recommends something, the text
   says recommends. Turning a recommendation into a requirement sends users off
   to fix a dataset that was never broken.
2. Never invent a specific value. Telling somebody their RepetitionTime "should
   be 2.0" is a guess about their scanner. The guidance says where to find the
   real value instead.
3. Distinguish an error from a warning everywhere. A warning is advice; an error
   means the dataset is not valid BIDS. Users routinely treat the two alike and
   either panic or ignore both.
4. Prefer the cause a user can act on. "The file is malformed" is true and
   useless; "your converter wrote milliseconds where BIDS wants seconds" is the
   same finding in a form somebody can fix.

Entries are built through ``add()``, which enforces the record shape the
retriever expects, and through the family helpers below, which exist because
roughly a third of the codes are the same finding applied to a different field.
Writing those out one at a time invites drift between explanations that ought to
be identical.
"""

from typing import Dict, List

# code -> enrichment block
ENRICHMENT: Dict[str, dict] = {}


def add(
    code: str,
    *,
    description: str,
    interpretation: str,
    why: str,
    causes: List[str],
    resolution: str,
    examples: List[str] = (),
    notes: str = "",
    confidence: str = "high",
) -> None:
    """Register one authored explanation, in the shape the retriever reads."""
    ENRICHMENT[code] = {
        "description": " ".join(description.split()),
        "interpretation": " ".join(interpretation.split()),
        "why_it_matters": " ".join(why.split()),
        "example_scenarios": [" ".join(e.split()) for e in examples],
        "common_causes": [" ".join(c.split()) for c in causes],
        "resolution_guidance": " ".join(resolution.split()),
        "additional_notes": " ".join(notes.split()),
        "confidence": confidence,
        "sources": [],
    }


# ============================================================
# Family 1: a time value that is almost certainly in milliseconds
#
# BIDS states timing metadata in SECONDS. Scanners and DICOM headers state much
# of it in milliseconds, so a converter that copies the number across without
# dividing produces a value that is valid JSON, valid against the schema's type
# rule, and wrong by a factor of a thousand. The check cannot prove the value is
# wrong, only that it is implausible, which is why every one of these is a
# warning rather than an error.
# ============================================================

def _seconds_family(code, field, threshold, plausible, extra_cause="") -> None:
    causes = [
        f"The value was copied straight from a DICOM header or scanner console "
        f"in milliseconds. BIDS requires seconds, so the number is 1000 times "
        f"too large.",
        f"A conversion script wrote {field} without applying the unit conversion "
        f"its source format needed.",
        f"The field was filled in by hand from a scanner protocol printout, "
        f"which usually reports milliseconds.",
    ]
    if extra_cause:
        causes.append(extra_cause)

    add(
        code,
        description=f"""
            {field} carries a value greater than {threshold}. BIDS expresses this
            field in seconds, and a genuine {field} above {threshold} seconds is
            rare enough that the validator asks you to confirm it.
        """,
        interpretation=f"""
            This is a warning, not an error: the dataset is still valid BIDS and
            the validator cannot tell a real long value from a unit mistake. It
            is flagging that the number looks like milliseconds. For most
            acquisitions {field} falls {plausible}.
        """,
        why=f"""
            Timing metadata is read by analysis software as seconds without
            further checking. A value that is 1000 times too large silently
            distorts anything derived from it, including slice timing
            correction, temporal filtering, design matrices and modelling of the
            haemodynamic response. Nothing downstream will warn you a second
            time.
        """,
        causes=causes,
        resolution=f"""
            Check the value against the acquisition protocol. If it was taken
            from a DICOM header in milliseconds, divide by 1000 and write the
            result back into the JSON sidecar. If the long value is genuinely
            correct for this acquisition, leave it as it is: the warning is a
            prompt to verify, not a defect to remove, and there is no way to
            suppress it other than correcting the value.
        """,
        examples=[
            f"A converter copies {field} from DICOM unchanged, so a sidecar "
            f"records the value in milliseconds where BIDS expects seconds.",
        ],
        notes="""
            BIDS states all timing metadata in seconds. Most sources of the same
            information, DICOM in particular, state it in milliseconds, so this
            class of mistake is common and worth checking across every timing
            field in the sidecar rather than only the one reported.
        """,
    )


_seconds_family(
    "ECHO_TIME_GREATER_THAN", "EchoTime", "1 second",
    "between roughly 0.002 and 0.1 seconds, that is 2 to 100 milliseconds",
)
_seconds_family(
    "REPETITION_TIME_GREATER_THAN", "RepetitionTime", "100 seconds",
    "between roughly 0.5 and 5 seconds for functional imaging, though sparse "
    "and anatomical sequences can be longer",
    extra_cause="""
        The acquisition really is a sparse or clustered-volume design with a long
        repetition time, in which case the value is correct and the warning can
        be confirmed and left alone.
    """,
)
_seconds_family(
    "TOTAL_READOUT_TIME_GREATER_THAN", "TotalReadoutTime", "10 seconds",
    "well under a second, typically a few tens of milliseconds",
)
_seconds_family(
    "POST_LABELING_DELAY_GREATER", "PostLabelingDelay", "10 seconds",
    "between roughly 0.5 and 3 seconds for arterial spin labelling",
)
_seconds_family(
    "LABELING_DURATION_GREATER", "LabelingDuration", "10 seconds",
    "between roughly 0.5 and 2 seconds for arterial spin labelling",
)
_seconds_family(
    "BOLUS_CUT_OFF_DELAY_TIME_GREATER", "BolusCutOffDelayTime", "10 seconds",
    "under a few seconds",
)


# ============================================================
# Family 2: a declared channel count disagrees with channels.tsv
#
# The sidecar states how many channels of each type a recording has, and
# channels.tsv lists them one per row. The two are written by different steps of
# most pipelines, so they drift. channels.tsv is the one to trust: it describes
# the file row by row, while the count is a summary that is easy to leave stale.
# ============================================================

def _channel_count_family(code, metadata_field, channel_type, level, note="") -> None:
    severity_sentence = (
        "This is an error, so the dataset is not valid BIDS until the two agree."
        if level == "error"
        else "This is a warning: the dataset is still valid BIDS, but one of the "
        "two statements about the recording is wrong and downstream software may "
        "read either of them."
    )

    add(
        code,
        description=f"""
            The {metadata_field} field in the JSON sidecar does not equal the
            number of rows in the associated channels.tsv whose type column is
            {channel_type}. The sidecar and the channel table disagree about how
            many {channel_type} channels the recording contains.
        """,
        interpretation=f"""
            {severity_sentence} channels.tsv is the more reliable of the two,
            because it describes the recording channel by channel, while
            {metadata_field} is a summary that is easy to write once and never
            update. Treat the table as the truth and the count as the thing to
            correct, unless you have a specific reason to believe otherwise.
        """,
        why=f"""
            Analysis software uses these counts to decide how to interpret a
            recording: which channels to load, which to treat as data and which
            as auxiliary. A wrong count can mean channels are silently dropped
            from an analysis, or that non-brain channels are analysed as though
            they were data. It is also the first sign that the sidecar describes a
            different recording than the one it sits beside, for example after
            files were copied between sessions.
        """,
        causes=[
            f"""
                Channels were added to or removed from the recording, for example
                by dropping bad channels, and channels.tsv was regenerated while
                the sidecar {metadata_field} was left at its original value.
            """,
            f"""
                The type column in channels.tsv does not spell the channel type
                as BIDS requires. The type MUST be upper case and drawn from the
                list the standard defines, so a row typed as "{channel_type.lower()}"
                or with a vendor-specific name is not counted as {channel_type}.
            """,
            f"""
                The sidecar was copied from another recording in the same study
                that had a different montage or a different number of auxiliary
                channels.
            """,
            f"""
                Auxiliary channels were counted into the wrong category, for
                example an eye channel recorded as EEG rather than EOG, so two
                counts are wrong at once.
            """,
        ],
        resolution=f"""
            Open the channels.tsv beside the recording and count the rows whose
            type column equals {channel_type}. If that number is right, set
            {metadata_field} in the JSON sidecar to it. If the number is wrong,
            the table is what needs correcting: check that each channel's type is
            spelled in upper case and uses one of the channel types the standard
            defines, since a misspelled type is not counted. Do not adjust the
            count to silence the message without establishing which of the two is
            actually correct.
        """,
        examples=[
            f"""
                A recording is preprocessed, two bad channels are removed and
                channels.tsv is rewritten from the cleaned data, but the sidecar
                still declares the original {metadata_field}.
            """,
        ],
        notes=note or """
            Channel types in channels.tsv must be upper case and must come from
            the set the BIDS standard defines. A type that merely looks right,
            such as a vendor label or a lower-case spelling, is not recognised
            and is therefore not counted.
        """,
    )


_channel_count_family("EEG_CHANNEL_COUNT_MISMATCH", "EEGChannelCount", "EEG", "warning")
_channel_count_family("ECG_CHANNEL_COUNT_MISMATCH", "ECGChannelCount", "ECG", "warning")
_channel_count_family("EMG_CHANNEL_COUNT_MISMATCH", "EMGChannelCount", "EMG", "warning")
_channel_count_family("EOG_CHANNEL_COUNT_MISMATCH", "EOGChannelCount", "EOG", "warning")
_channel_count_family("MISC_CHANNEL_COUNT_MISMATCH", "MiscChannelCount", "MISC", "warning")
_channel_count_family("TRIGGER_CHANNEL_COUNT_MISMATCH", "TriggerChannelCount", "TRIG", "warning")
_channel_count_family("ACCEL_CHANNEL_COUNT", "ACCELChannelCount", "ACCEL", "error")
_channel_count_family("GYRO_CHANNEL_COUNT", "GYROChannelCount", "GYRO", "error")
_channel_count_family("MAGN_CHANNEL_COUNT", "MAGNChannelCount", "MAGN", "error")
_channel_count_family(
    "NIRS_CHANNEL_COUNT", "NIRSChannelCount", "NIRS", "error",
    note="""
        NIRS channel types begin with NIRS, for example NIRSCWAMPLITUDE. The
        count covers every channel whose type starts with that prefix, not only
        an exact match on the word NIRS.
    """,
)


# ============================================================
# Family 3: an array of per-volume values is the wrong length
#
# Several sidecar fields hold one value per acquired volume. If the array length
# does not match the number of volumes, there is no way to know which value
# belongs to which volume, so the mapping is not merely imprecise, it is absent.
# ============================================================

def _length_family(code, field, counted_against, level, guidance_extra="") -> None:
    severity_sentence = (
        "This is an error: the dataset is not valid BIDS until the lengths match."
        if level == "error"
        else "This is a warning: the dataset remains valid BIDS, but the "
        "mismatch usually indicates a real bookkeeping mistake."
    )

    add(
        code,
        description=f"""
            The number of values in {field} does not match {counted_against}.
            {field} holds one value per volume, so its length has to equal the
            number of volumes it describes.
        """,
        interpretation=f"""
            {severity_sentence} The check is comparing two independent statements
            about how many volumes the file contains and finding they disagree.
            One of them is wrong, and the validator cannot tell which.
        """,
        why=f"""
            When the array length is wrong, there is no correct way to pair its
            values with the volumes. Software either fails outright or, worse,
            pairs them by position and silently attributes the wrong parameter to
            every volume after the first discrepancy. Results computed that way
            look plausible and are wrong.
        """,
        causes=[
            """
                Volumes were removed from the image after the sidecar was
                written, for example dummy scans dropped during preprocessing,
                without the corresponding entries being removed from the array.
            """,
            """
                The acquisition was stopped early or restarted, so fewer volumes
                were saved than the protocol planned, while the sidecar still
                describes the planned acquisition.
            """,
            """
                The sidecar was copied from a similar run with a different number
                of volumes.
            """,
            """
                A single value was written where one value per volume is
                required, or one value per volume was written where a single
                value was expected.
            """,
        ],
        resolution=f"""
            Count the volumes in the image itself, then count the values in
            {field}, and establish which of the two reflects what was actually
            acquired. Correct whichever is wrong rather than padding or trimming
            the array to make the numbers agree, because padding produces values
            that are the right length and describe nothing real.
            {guidance_extra}
        """,
        examples=[
            f"""
                Four dummy volumes are discarded from a run, so the image now has
                four fewer volumes than {field} has values.
            """,
        ],
        notes="""
            When a field may hold either one value for the whole file or one
            value per volume, both forms are legitimate. The mismatch is only
            between the per-volume form and a volume count, so switching to the
            single-value form is a valid fix when the parameter really was
            constant across the acquisition.
        """,
    )


_length_family(
    "ASLCONTEXT_TSV_NOT_CONSISTENT",
    "the aslcontext.tsv volume list",
    "the number of volumes in the NIfTI image",
    "error",
    guidance_extra="""
        aslcontext.tsv must have one row per volume in the ASL image, in
        acquisition order, plus its header row.
    """,
)
_length_family(
    "FLIP_ANGLE_NOT_MATCHING_ASLCONTEXT_TSV", "FlipAngle",
    "the number of volumes listed in the associated aslcontext.tsv", "error",
)
_length_family(
    "FLIP_ANGLE_NOT_MATCHING_NIFTI", "FlipAngle",
    "the fourth dimension of the NIfTI header", "error",
)
_length_family(
    "POST_LABELING_DELAY_NOT_MATCHING_ASLCONTEXT_TSV", "PostLabelingDelay",
    "the number of volumes listed in the associated aslcontext.tsv", "error",
)
_length_family(
    "POST_LABELING_DELAY_NOT_MATCHING_NIFTI", "PostLabelingDelay",
    "the fourth dimension of the NIfTI header", "error",
)
_length_family(
    "LABELING_DURATION_LENGTH_NOT_MATCHING_NIFTI", "LabelingDuration",
    "the fourth dimension of the NIfTI header", "error",
)
_length_family(
    "LABELLING_DURATION_NOT_MATCHING_ASLCONTEXT_TSV", "LabelingDuration",
    "the number of volumes listed in the associated aslcontext.tsv", "error",
)
_length_family(
    "REPETITIONTIMEPREPARATION_NOT_MATCHING_ASLCONTEXT_TSV",
    "RepetitionTimePreparation",
    "the number of volumes listed in the associated aslcontext.tsv", "error",
)
_length_family(
    "REPETITIONTIME_PREPARATION_NOT_CONSISTENT", "RepetitionTimePreparation",
    "the fourth dimension of the NIfTI header", "error",
)
_length_family(
    "ECHO_TIME_NOT_CONSISTENT", "EchoTime",
    "the number of volumes in the associated aslcontext.tsv", "warning",
)
_length_family(
    "BACKGROUND_SUPPRESSION_PULSE_NUMBER_NOT_CONSISTENT",
    "BackgroundSuppressionPulseTime",
    "the BackgroundSuppressionNumberPulses field", "warning",
    guidance_extra="""
        BackgroundSuppressionNumberPulses states how many suppression pulses were
        played, and BackgroundSuppressionPulseTime lists when each one occurred,
        so the list must hold exactly that many times.
    """,
)
_length_family(
    "TOTAL_ACQUIRED_VOLUMES_NOT_CONSISTENT", "TotalAcquiredPairs",
    "the number of control-label pairs listed in the associated aslcontext.tsv",
    "warning",
)
_length_family(
    "PDT2_ECHOS_SHOULD_MATCH_NIFTI_LENGTH", "EchoTime",
    "the number of volumes in the PDT2 image", "warning",
    guidance_extra="""
        A PDT2 file holds a proton-density volume and a T2-weighted volume, so
        EchoTime should carry one value for each.
    """,
)
_length_family(
    "PET_FRAME_CONSISTENCY_FRAME_DURATION", "FrameDuration",
    "the number of frames in the PET image", "error",
    guidance_extra="""
        PET frames are unevenly spaced by design, so the arrays cannot be
        reconstructed from a start time and a step. Both FrameDuration and
        FrameTimesStart must list every frame explicitly.
    """,
)
_length_family(
    "PET_FRAME_CONSISTENCY_FRAME_TIMES_START", "FrameTimesStart",
    "the number of frames in the PET image", "error",
    guidance_extra="""
        PET frames are unevenly spaced by design, so the arrays cannot be
        reconstructed from a start time and a step. Both FrameDuration and
        FrameTimesStart must list every frame explicitly.
    """,
)
_length_family(
    "PET_FRAME_CONSISTENCY", "FrameDuration and FrameTimesStart",
    "each other", "error",
    guidance_extra="""
        Both fields describe the same frames, so they must have the same number
        of entries even before either is compared against the image.
    """,
)
_length_family(
    "SLICETIMING_ELEMENTS", "SliceTiming",
    "the number of slices in the corresponding NIfTI volume", "warning",
    guidance_extra="""
        SliceTiming holds the acquisition time of every slice within a volume, so
        it needs exactly one value per slice along the slice axis, not per
        volume.
    """,
)


# ============================================================
# File integrity: the validator could not read the file at all
#
# These fire before any BIDS rule is considered. The file is unreadable,
# truncated, mislabelled or absent, so nothing about its contents can be
# checked. They are almost always an artefact of how the data was copied rather
# than anything to do with the standard.
# ============================================================

add(
    "EMPTY_FILE",
    description="""
        The file exists but contains no data. BIDS does not allow empty files
        anywhere in a dataset.
    """,
    interpretation="""
        This is an error. An empty file is indistinguishable from a file that was
        never written, but it is worse than a missing file, because its presence
        asserts that data exists where none does.
    """,
    why="""
        A zero-length file breaks every tool that opens it, and it hides the real
        problem: something upstream failed silently. It also makes a dataset look
        complete in a file listing when it is not, which is how an incomplete
        dataset ends up shared or archived.
    """,
    causes=[
        "A conversion or export step failed part way and left the output file behind.",
        "A copy or transfer was interrupted, or ran out of disk space.",
        "A placeholder file was created by hand and never filled in.",
        "A version-control or data-sharing tool fetched only file metadata, not content.",
    ],
    resolution="""
        Find out whether the data exists elsewhere. If it does, copy it in again
        and confirm the file size is non-zero. If it does not, delete the empty
        file rather than leaving it: a dataset that is honestly missing a
        recording is valid, one that claims a recording it does not have is not.
    """,
    examples=[
        "A conversion is cancelled mid-run, leaving a zero-byte NIfTI beside the completed ones.",
    ],
)

add(
    "FILE_READ",
    description="""
        The validator could not read the file. It may be corrupt, truncated,
        locked by another process, or not the kind of file its extension claims.
    """,
    interpretation="""
        This is an error, and it is about the file itself rather than about BIDS.
        No rule could be evaluated, so a clean report for this file means nothing
        until the file can be read.
    """,
    why="""
        An unreadable file will fail in exactly the same way for every analysis
        tool that comes after the validator. Finding it now, while the source data
        is probably still available, is far cheaper than finding it during
        analysis.
    """,
    causes=[
        "The file is truncated because a copy or download did not finish.",
        "The storage medium or transfer corrupted the file.",
        "File permissions prevent reading, or another process holds the file open.",
        "The extension does not match the actual format, so the reader for that extension fails.",
    ],
    resolution="""
        Check the file size against the original, verify a checksum if you have
        one, and confirm you can open the file with the tool that normally reads
        that format. Re-copy it from the source if it is damaged. If the file
        reads correctly by hand, check its permissions.
    """,
)

add(
    "GZ_NOT_GZIPPED",
    description="""
        The filename ends in .gz but the contents are not gzip-compressed data.
    """,
    interpretation="""
        This is an error. The extension is a promise about the format, and every
        tool that opens the file will try to decompress it and fail.
    """,
    why="""
        Software selects a reader from the extension. A plain NIfTI named .nii.gz
        will be handed to a gzip reader, which fails immediately, so the file is
        unusable despite containing perfectly good data.
    """,
    causes=[
        """
            A file was decompressed and the .gz extension was left on the
            resulting name.
        """,
        """
            A file was renamed to add .gz without actually compressing it,
            usually to satisfy a naming convention.
        """,
        """
            A transfer step decompressed the file automatically in flight, which
            some web servers and sync tools do, while preserving the name.
        """,
    ],
    resolution="""
        Determine whether the contents are compressed. If they are not, either
        compress the file with gzip so the name becomes true, or remove the .gz
        from the name if the uncompressed extension is permitted for that file
        type. Do not rename to .gz without compressing.
    """,
)

add(
    "NIFTI_TOO_SMALL",
    description="""
        The file is smaller than the minimum size a NIfTI header occupies, so it
        cannot be a valid NIfTI image whatever it contains.
    """,
    interpretation="""
        This is an error, and it is a size check rather than a content check: the
        file is too short to hold even the header, let alone image data.
    """,
    why="""
        A NIfTI file this small holds no usable image. It is the signature of a
        conversion or transfer that failed and left a stub behind, which will
        otherwise be discovered only when an analysis tries to load it.
    """,
    causes=[
        "The conversion producing the file failed or was interrupted.",
        "The transfer or copy was truncated.",
        "The file is a pointer, stub or placeholder from a data-sharing tool rather than the real content.",
    ],
    resolution="""
        Re-create the file from its source data, or re-fetch it if the dataset
        uses a tool that stores content separately from the file tree. Compare
        the resulting file size against a comparable image from the same study to
        confirm it is plausible.
    """,
)

add(
    "NIFTI_HEADER_UNREADABLE",
    description="""
        The file is large enough to hold a NIfTI header, but the header could not
        be parsed.
    """,
    interpretation="""
        This is an error. The file is either not a NIfTI image, or it is one that
        has been corrupted or truncated in the header region.
    """,
    why="""
        Everything a tool needs to interpret the image, its dimensions, voxel
        sizes, orientation and data type, lives in the header. An unreadable
        header makes the voxel data meaningless even when the voxel data itself
        is intact.
    """,
    causes=[
        "The file is corrupt or was truncated during copying.",
        "The file is a different format that has been given a NIfTI extension.",
        "A compressed file is damaged, so decompression yields garbage where the header should be.",
        "A partially written file from an interrupted conversion.",
    ],
    resolution="""
        Try opening the file with a NIfTI reader directly to confirm the
        diagnosis. Re-convert from the original source data if it is available,
        since a damaged header generally cannot be repaired. Check whether other
        files from the same conversion run share the problem, which points at the
        conversion rather than at this one file.
    """,
)

add(
    "JSON_INVALID",
    description="""
        The file is not valid JSON and could not be parsed.
    """,
    interpretation="""
        This is an error. Because the file cannot be parsed, none of the metadata
        it contains is available to any check, so a JSON error usually suppresses
        many other findings that will appear once it is fixed.
    """,
    why="""
        Sidecars carry the metadata that makes a recording interpretable. An
        unparseable sidecar means the recording has no metadata at all as far as
        every downstream tool is concerned, even though the information is
        visibly present in the file.
    """,
    causes=[
        """
            A trailing comma after the last entry in an object or array. This is
            the single most common cause and is accepted by some editors.
        """,
        "A quote, brace or bracket that was never closed.",
        "Single quotes instead of double quotes, or unquoted keys, which JSON does not permit.",
        """
            Python or JavaScript literals written into JSON: None, True, False,
            NaN or Infinity rather than null, true and false.
        """,
        "Two sidecars concatenated, or a file edited by two processes at once.",
    ],
    resolution="""
        Run the file through any JSON parser or linter; it will report the line
        and column of the first problem, which is the fastest way in. Fix the
        syntax and validate again, and expect new findings to appear afterwards
        that were previously hidden behind the parse failure.
    """,
    examples=[
        """
            A sidecar ends with "EchoTime": 0.03, followed immediately by the
            closing brace. The trailing comma makes the whole file unparseable.
        """,
    ],
)

add(
    "INVALID_JSON_ENCODING",
    description="""
        A JSON file is not encoded in UTF-8. BIDS requires UTF-8 for JSON files.
    """,
    interpretation="""
        This is an error. The file may look correct in the editor that wrote it
        while being unreadable to tools that follow the standard.
    """,
    why="""
        Encoding determines how bytes become characters. A file in another
        encoding either fails to parse or parses with corrupted text, which
        matters most for the free-text fields where names, institutions and
        descriptions live, and where non-ASCII characters actually occur.
    """,
    causes=[
        """
            The file was saved in a legacy regional encoding such as Latin-1 or
            Windows-1252, which is still the default in some editors and
            spreadsheet exports.
        """,
        "A UTF-16 file, which some Windows tools produce by default.",
        """
            Text with accented characters was pasted from an application that
            used a different encoding.
        """,
    ],
    resolution="""
        Re-save the file as UTF-8. Most editors expose the encoding in the save
        dialogue or a status bar. Afterwards, check that accented characters and
        other non-ASCII text still read correctly, since a wrong-encoding
        conversion can corrupt them silently.
    """,
    notes="""
        A UTF-8 byte order mark at the start of the file is a frequent secondary
        cause of trouble: the encoding is right but the leading bytes break some
        strict parsers. Saving as UTF-8 without a byte order mark avoids it.
    """,
)

add(
    "JSON_SCHEMA_VALIDATION_ERROR",
    description="""
        The JSON file parses correctly but does not conform to the structure BIDS
        defines for it. A field has the wrong type, an unexpected shape, or a
        value outside what the standard permits.
    """,
    interpretation="""
        This is an error about content rather than syntax. The distinction
        matters: the file is readable, so the fix is to a specific field, not to
        the file's punctuation. The accompanying message names the field.
    """,
    why="""
        Type declarations are what let software consume metadata without guessing.
        A number written as a string, or a single value where a list is required,
        forces every reader either to fail or to apply its own coercion rules,
        and different tools coerce differently.
    """,
    causes=[
        """
            A number written as a string, for example "2.0" rather than 2.0. This
            is common when metadata passes through a spreadsheet or a text
            template.
        """,
        "A single value where the standard requires an array, or an array where it requires a single value.",
        "A value outside the set the standard allows for that field.",
        "A misspelled field name, which can make a value appear under the wrong definition.",
    ],
    resolution="""
        Read the message for the field name and the expected type, then look up
        that field's definition to see what it accepts. Correct the value's type
        or shape in the sidecar. Where the value came from an automated
        conversion, fix it at the source so sibling files do not carry the same
        defect.
    """,
)

add(
    "WRONG_NEW_LINE",
    description="""
        A TSV file uses carriage return characters to end its lines. BIDS
        requires the line feed character.
    """,
    interpretation="""
        This is an error, and it is invisible in most editors, which display both
        line endings identically. The file looks completely normal while being
        non-conformant.
    """,
    why="""
        Parsers that split strictly on line feed treat a carriage return as part
        of the last field on each line. A column of values silently gains a
        trailing invisible character, so comparisons against it fail for reasons
        nobody can see.
    """,
    causes=[
        """
            The file was written or edited on Windows, where the convention is
            carriage return plus line feed.
        """,
        """
            The file was exported from a spreadsheet application, which commonly
            uses the platform convention.
        """,
        """
            A version control system converted line endings on checkout.
        """,
    ],
    resolution="""
        Convert the file's line endings to line feed only. Most editors offer
        this as a setting, often shown in the status bar, and dedicated tools
        exist for it. If the file came from a spreadsheet, check the export
        options, and configure version control not to rewrite line endings for
        .tsv files.
    """,
)

add(
    "DUPLICATE_FILES",
    description="""
        The same file exists twice, once compressed and once not, for example as
        both image.nii and image.nii.gz.
    """,
    interpretation="""
        This is an error. Nothing in the standard says which of the two is
        authoritative, so software picking one is making a choice the dataset
        never expressed.
    """,
    why="""
        Two files with the same BIDS name are the same recording as far as the
        standard is concerned. Different tools will resolve the ambiguity
        differently, and if the two files ever diverge in content, results become
        dependent on which tool read which copy.
    """,
    causes=[
        """
            A file was compressed or decompressed and the original was not
            removed.
        """,
        "Two conversion runs wrote to the same directory with different compression settings.",
        "A backup or working copy was left in the dataset tree.",
    ],
    resolution="""
        Keep one of the two and delete the other. Compressed images are the usual
        choice for imaging data. Before deleting, confirm the two really hold the
        same data, since a leftover from an earlier conversion may differ from
        the current one.
    """,
)

add(
    "ORPHANED_SYMLINK",
    description="""
        The file is a symbolic link whose target does not exist.
    """,
    interpretation="""
        This is an error. The dataset appears to contain the file, but opening it
        yields nothing.
    """,
    why="""
        A broken link is worse than a missing file, because a file listing shows
        it as present. Datasets look complete, are shared, and then fail for
        whoever receives them.
    """,
    causes=[
        """
            The dataset was copied without following links, so the links came
            across but their targets did not.
        """,
        """
            The link points outside the dataset to a location that exists only on
            the machine where it was created.
        """,
        "The target was moved, renamed or deleted after the link was made.",
        """
            The dataset uses a content-addressed data-management tool and the
            file content has not been fetched.
        """,
    ],
    resolution="""
        Find where the link points and decide whether the target should be inside
        the dataset. For sharing, replace links with the real files, since links
        to locations outside the dataset never survive transfer. If the dataset
        is managed by a tool that stores content separately, fetch the content
        before validating.
    """,
)

add(
    "INACCESSIBLE_REMOTE_FILE",
    description="""
        The file is a link to content held remotely by a data-management tool,
        and that content could not be retrieved.
    """,
    interpretation="""
        This is an error, but it often reflects the state of your local copy
        rather than a defect in the dataset. The dataset structure may be
        entirely correct while the file contents are simply not present yet.
    """,
    why="""
        Checks that read file contents cannot run without the contents. A
        validation report produced against unfetched files is silent about
        anything inside them, so it can look clean while saying almost nothing.
    """,
    causes=[
        "The file content has not been fetched into the local copy yet.",
        "The remote store is unreachable, or requires credentials that are not configured.",
        "The content is no longer available at the remote location.",
    ],
    resolution="""
        Fetch the content with whatever tool manages the dataset, then validate
        again. If fetching fails, check network access and credentials for the
        remote store. If you intend to validate structure only, be aware that
        content-dependent checks did not run.
    """,
)

add(
    "INTERNAL_ERROR",
    description="""
        The validator encountered an unexpected condition and stopped part of its
        work. Some validation steps did not run.
    """,
    interpretation="""
        This is an error in the validator rather than a statement about your
        dataset. The important consequence is that the report is incomplete: a
        clean result elsewhere does not mean the dataset passed those checks,
        only that they were not reached.
    """,
    why="""
        An incomplete validation that looks complete is misleading. Treating the
        rest of the report as authoritative can send a dataset onward with real
        problems that were never examined.
    """,
    causes=[
        "A file in a shape the validator did not anticipate.",
        "A version mismatch between the validator and the schema it is using.",
        "Resource limits, such as memory, on a very large dataset.",
    ],
    resolution="""
        Update to a current validator version and run again. If it recurs,
        narrow the problem by validating subsets until one file reproduces it,
        then report that file to the validator's maintainers. Do not treat the
        rest of the report as a complete result.
    """,
)

add(
    "NOT_INCLUDED",
    description="""
        A file's name does not match any naming pattern the BIDS specification
        defines, so the standard has no rules to apply to it.
    """,
    interpretation="""
        This is an error. It does not mean the file is bad, only that BIDS does
        not recognise it. A file the standard cannot name is a file no BIDS tool
        can find.
    """,
    why="""
        BIDS works because filenames are machine-readable. Tools locate data by
        constructing names from entities rather than by searching, so an
        unrecognised name is invisible to them regardless of what it contains.
    """,
    causes=[
        """
            A typo or wrong case in an entity or suffix. Entity keys and suffixes
            are case-sensitive, so T1W is not T1w.
        """,
        """
            Entities in the wrong order. BIDS fixes the order of entities in a
            filename, and a correct set in the wrong sequence is still invalid.
        """,
        """
            An entity that is not permitted for that datatype and suffix, or a
            required entity left out.
        """,
        """
            Source data, notes, logs or derived files placed inside the raw
            dataset where the standard does not define them.
        """,
        """
            A file the standard genuinely does not cover, which belongs in
            sourcedata/, derivatives/ or an entry in .bidsignore.
        """,
    ],
    resolution="""
        Compare the name against the naming template for its datatype and suffix,
        checking spelling, case and entity order. If the file is a legitimate part
        of the dataset that BIDS does not define, move it to sourcedata/ or
        derivatives/ as appropriate, or list it in .bidsignore so the validator
        knows it is deliberate. Use .bidsignore for files that genuinely fall
        outside the standard, not to silence names that could be corrected.
    """,
)

add(
    "SIDECAR_WITHOUT_DATAFILE",
    description="""
        A JSON sidecar was found with no corresponding data file.
    """,
    interpretation="""
        This is an error. A sidecar describes a specific recording, and with the
        recording absent it describes nothing.
    """,
    why="""
        An orphaned sidecar usually means a data file was lost, moved or renamed
        without its metadata following. That is worth knowing: the missing item is
        the recording, and the sidecar is the evidence it was once there.
    """,
    causes=[
        """
            The data file was renamed and the sidecar was not, so the two no
            longer share a name.
        """,
        "The data file was deleted or was never copied across.",
        """
            A typo in either name breaks the match, since a sidecar is paired to
            its data file by exact name.
        """,
        """
            A sidecar intended to be inherited by several files was placed at the
            level of a single recording rather than higher in the hierarchy.
        """,
    ],
    resolution="""
        Decide which of the two is wrong. If the recording exists under a
        different name, rename the sidecar to match it exactly. If the recording
        is genuinely gone, delete the sidecar. If the sidecar was meant to apply
        to several recordings, move it up to the subject, session or dataset level
        where the inheritance principle will apply it to all of them.
    """,
)

add(
    "NO_VALID_DATA_FOUND_FOR_SUBJECT",
    description="""
        A subject directory exists but contains no files the BIDS specification
        recognises.
    """,
    interpretation="""
        This is an error. The subject is declared by the presence of its
        directory, but nothing inside it is readable as BIDS data.
    """,
    why="""
        A subject with no valid data will be counted by anything that enumerates
        subjects and then contribute nothing, which produces analyses with a
        participant count that does not match the data analysed.
    """,
    causes=[
        """
            Every file in the subject has a naming problem, usually the same one,
            pointing at a conversion that went wrong for this subject.
        """,
        "The data sits one directory level too deep, or too shallow.",
        "The directory holds only source data or notes, not converted data.",
        "The directory is an empty leftover from a subject that was removed.",
    ],
    resolution="""
        Look at what is actually inside the directory. If files are present but
        misnamed, correct the names, since fixing one usually fixes all of them.
        If the data sits at the wrong depth, move it so datatype directories are
        directly under the subject or session directory. If the subject has no
        data, remove the directory and the corresponding participants.tsv row.
    """,
)

add(
    "BRAINVISION_LINKS_BROKEN",
    description="""
        A BrainVision recording consists of three files, a header .vhdr, a marker
        .vmrk and the data .eeg. The internal pointers between them do not resolve
        to the files that are present.
    """,
    interpretation="""
        This is an error. The BrainVision header stores the names of its two
        companion files as text inside the header, so renaming any of the three
        breaks the set even though all three are sitting in the directory.
    """,
    why="""
        Readers follow the names written in the header, not the names on disk. A
        recording whose links are broken cannot be opened at all, which is a
        particularly easy mistake to make in BIDS because converting a dataset
        means renaming every file.
    """,
    causes=[
        """
            The files were renamed to BIDS entities without updating the
            DataFile and MarkerFile entries inside the .vhdr, and the MarkerFile
            entry inside the .vmrk.
        """,
        "One of the three files was not copied.",
        """
            The files were renamed by a tool that changed the names on disk
            without rewriting the header text.
        """,
    ],
    resolution="""
        Open the .vhdr in a text editor and check that its DataFile and
        MarkerFile entries name the .eeg and .vmrk files exactly as they exist on
        disk, then check the .vmrk names the .eeg the same way. Correct the text
        to match. Conversion tools built for BrainVision rewrite these pointers
        automatically, so converting again with a proper tool is usually safer
        than editing by hand.
    """,
    notes="""
        All three files must share the same BIDS name and differ only in
        extension. The .eeg and .vmrk are never opened directly; the .vhdr is the
        entry point.
    """,
)


# ============================================================
# Image geometry and dimensionality
# ============================================================

add(
    "BOLD_NOT_4D",
    description="""
        A file with the bold suffix is not four-dimensional. A BOLD run is a time
        series, so it must have three spatial dimensions plus time.
    """,
    interpretation="""
        This is an error. A three-dimensional file named as bold is either a
        single volume that should not carry that suffix, or a time series that
        lost its time dimension somewhere in conversion.
    """,
    why="""
        Every functional analysis assumes it can index the fourth dimension. A
        three-dimensional bold file fails immediately in some tools and is
        misread as a single timepoint by others, which produces an analysis with
        no temporal information and no complaint.
    """,
    causes=[
        """
            The conversion split a run into one file per volume instead of
            stacking them, which happens when the source series is not recognised
            as a time series.
        """,
        """
            The file really is a single volume, such as a single-band reference
            image, and has been given the bold suffix instead of its own.
        """,
        """
            A run was aborted after one volume was acquired, so the data is
            genuinely three-dimensional.
        """,
        """
            A preprocessing step that averages across time, such as computing a
            mean image, wrote its output back into the raw dataset.
        """,
    ],
    resolution="""
        Check how many volumes the image has. If the run was split into
        single-volume files, re-run the conversion so the volumes are stacked
        into one file. If the file is a reference image rather than a time
        series, give it the suffix that describes what it is, since a
        single-band reference has its own. If it is a derived image such as a
        temporal mean, move it into derivatives/ rather than leaving it among the
        raw data.
    """,
)

add(
    "T1W_FILE_WITH_TOO_MANY_DIMENSIONS",
    description="""
        A file with the T1w suffix has more than three dimensions. An anatomical
        T1-weighted image is a single volume.
    """,
    interpretation="""
        This is an error. The extra dimension means the file holds several
        images stacked together, which the T1w suffix does not describe.
    """,
    why="""
        Anatomical images are used as a reference space for everything else:
        registration targets, segmentation inputs, surface reconstruction. Tools
        expect one volume and will either fail or silently use only the first,
        so which image actually served as the anatomical reference becomes
        unclear.
    """,
    causes=[
        """
            Several repetitions or averages were stacked into one file rather
            than saved separately or averaged into one volume.
        """,
        """
            A multi-echo or multi-inversion acquisition was written as one file,
            where BIDS separates the parts with the echo or inv entity.
        """,
        """
            A different acquisition, such as a quantitative MRI series, was given
            the T1w suffix. A file collection of that kind has its own suffixes
            and entities.
        """,
        """
            A conversion merged several distinct series that happened to share a
            series description.
        """,
    ],
    resolution="""
        Look at the size of the fourth dimension, which tells you how many images
        were stacked. If they are repetitions, either split them into separate
        files distinguished by the run entity, or average them into the single
        volume you intend to use. If they are echoes or inversions, split them and
        label them with the echo or inv entity. If the acquisition is a
        quantitative MRI file collection, use the suffix defined for it rather
        than T1w.
    """,
)

add(
    "MAGNITUDE_FILE_WITH_TOO_MANY_DIMENSIONS",
    description="""
        A magnitude1 or magnitude2 fieldmap image has more than three dimensions.
        Each magnitude image is a single volume.
    """,
    interpretation="""
        This is an error. The two magnitude images of a fieldmap belong in
        separate files, one per echo, not stacked into one.
    """,
    why="""
        Fieldmap correction pairs each magnitude image with a specific echo time.
        Stacking them removes the correspondence between file and echo, so the
        correction cannot be set up correctly.
    """,
    causes=[
        """
            The conversion stacked both echoes into one file rather than writing
            magnitude1 and magnitude2 separately.
        """,
        "A multi-echo series was written as a single four-dimensional file.",
    ],
    resolution="""
        Split the file so that each echo becomes its own image, named magnitude1
        and magnitude2, and confirm that EchoTime1 and EchoTime2 in the phasediff
        sidecar correspond to them in that order.
    """,
)

add(
    "PDT2_FILE_SHOULD_HAVE_TWO_VOLUMES",
    description="""
        A PDT2 file should be four-dimensional with exactly two volumes: the
        proton-density image and the T2-weighted image acquired together.
    """,
    interpretation="""
        This is a warning. The PDT2 suffix specifically describes a dual-echo
        acquisition that yields those two contrasts, so a file with a different
        number of volumes probably is not one.
    """,
    why="""
        Anything consuming a PDT2 file expects to find both contrasts and to know
        which is which by position. A different volume count makes that
        assumption unsafe.
    """,
    causes=[
        """
            Only one of the two contrasts was converted, in which case it should
            carry its own suffix rather than PDT2.
        """,
        "Additional volumes from the same series were included in the file.",
        "The suffix was applied to an acquisition that is not a dual-echo PD/T2.",
    ],
    resolution="""
        Check what the volumes actually contain. If the file holds only one
        contrast, give it the suffix for that contrast instead. If it holds the
        two expected volumes plus extras, remove the extras. Confirm that
        EchoTime carries one value per volume.
    """,
)

add(
    "NIFTI_DIMENSION",
    description="""
        The dimension field of the NIfTI header is blank or shorter than the image
        requires, so the header does not fully describe the image's shape.
    """,
    interpretation="""
        This is a warning. The image data may be intact while the header fails to
        describe it, which means tools have to guess.
    """,
    why="""
        Voxel data in a NIfTI file is a flat block of numbers. Only the header
        says how to fold it into a volume. An underspecified dimension field means
        different readers may fold it differently.
    """,
    causes=[
        "The file was written by a tool that did not populate the header fully.",
        "The header was edited or copied from another image.",
        "The image was produced by a conversion from a format that carries less geometry information.",
    ],
    resolution="""
        Re-convert from the original source data if possible, since a conversion
        tool that reads the source geometry will write a complete header. If the
        image cannot be re-converted, set the dimension fields explicitly using a
        NIfTI toolkit, taking the values from the acquisition protocol rather
        than guessing.
    """,
)

add(
    "NIFTI_PIXDIM",
    description="""
        The NIfTI header reports voxel sizes of zero. A voxel with no extent
        cannot describe a real image.
    """,
    interpretation="""
        This is a warning about the header rather than the voxel data. The image
        may display correctly while carrying no usable information about scale.
    """,
    why="""
        Voxel size is how anatomical distance is recovered from array indices.
        With zero voxel sizes, spatial smoothing, registration, volume
        measurement and any report in millimetres are all either impossible or
        wrong.
    """,
    causes=[
        "The writing tool left the voxel size fields unset.",
        "A header was constructed by hand or copied from an incompatible image.",
        "A conversion from a source format that did not expose voxel dimensions.",
    ],
    resolution="""
        Re-convert from the original data, which is the reliable fix because the
        source carries the true voxel geometry. Only if that is impossible,
        set the voxel dimensions from the acquisition protocol with a NIfTI
        toolkit, and record that the geometry was reconstructed rather than read.
    """,
)

add(
    "NIFTI_PIXDIM_PET",
    description="""
        A PET image reports voxel sizes of zero along its spatial axes.
    """,
    interpretation="""
        This is a warning with the same meaning as a zero voxel size in any other
        image, reported separately because PET images are checked only on their
        spatial axes.
    """,
    why="""
        Quantitative PET analysis depends on knowing the volume each voxel
        represents. Without voxel sizes, concentrations, regional statistics and
        any coregistration with an anatomical image are unreliable.
    """,
    causes=[
        "The reconstruction or conversion did not carry voxel geometry into the NIfTI header.",
        "The image was converted from a format whose geometry was not read correctly.",
    ],
    resolution="""
        Re-convert from the original reconstruction output using a tool that
        reads its geometry. If the image must be repaired in place, take the voxel
        sizes from the reconstruction parameters, not from a similar scan.
    """,
)

add(
    "NIFTI_UNIT",
    description="""
        The NIfTI header does not fully specify the units for the spatial and
        temporal dimensions.
    """,
    interpretation="""
        This is a warning. Readers will apply a default, usually millimetres and
        seconds, which is normally right but is an assumption rather than a fact
        the file states.
    """,
    why="""
        If an image was written in different units and the header does not say so,
        every spatial measurement taken from it is wrong by a constant factor,
        and nothing in the file reveals it.
    """,
    causes=[
        "The writing tool left the units field unset.",
        "A conversion that did not propagate unit information.",
    ],
    resolution="""
        Set the units field with a NIfTI toolkit after confirming what the image
        actually uses, or re-convert from source. Before doing either, check that
        the voxel sizes are plausible when read as millimetres, since that is the
        assumption everything downstream is already making.
    """,
)

add(
    "SFORM_AND_QFORM_IN_IMAGE_HEADER_ARE_ZERO",
    description="""
        Both orientation codes in the NIfTI header, sform_code and qform_code, are
        zero, so the file declares no relationship between its voxel grid and any
        anatomical coordinate system.
    """,
    interpretation="""
        This is a warning, and it is one of the more consequential ones. The image
        has no stated orientation at all, so nothing can determine which side is
        left.
    """,
    why="""
        Left and right are not recoverable from voxel data. When both orientation
        codes are zero, software falls back to its own assumption, and different
        tools assume differently. A left-right flip introduced this way survives
        into every result and is essentially undetectable in a brain image, which
        makes it one of the few errors capable of reversing a lateralised
        finding without anyone noticing.
    """,
    causes=[
        "The image was written by a tool that did not set the orientation fields.",
        "A conversion from a format that did not carry orientation information.",
        "A header was stripped deliberately, sometimes as a misguided anonymisation step.",
    ],
    resolution="""
        Re-convert from the original source data, which is the only route that
        recovers the true orientation. If that is impossible, the orientation must
        be established from external evidence, for example a scan with known
        laterality or a marker visible in the image, and not assumed. Record how
        it was determined. Do not simply set an orientation code to a plausible
        value, because that replaces an honest absence of information with a
        confident claim that may be wrong.
    """,
    notes="""
        Of the two, sform is the one most analysis software reads. Both being
        zero is the worst case; one being set is usually workable.
    """,
)

add(
    "NIFTI_PE_DIRECTION_CONSISTENCY",
    description="""
        The PhaseEncodingDirection in the sidecar and the image's own orientation
        imply a phase-encoding axis that does not match the value of the dir
        entity in the filename.
    """,
    interpretation="""
        This is a warning about a disagreement between the filename and the
        metadata. Two independent statements are being made about the same fact
        and they do not agree.
    """,
    why="""
        Susceptibility distortion correction reverses distortion along the
        phase-encoding axis and relies on knowing its direction. Getting the
        direction wrong does not fail: it applies the correction backwards,
        roughly doubling the distortion it was meant to remove.
    """,
    causes=[
        """
            The dir entity was assigned by hand or by convention rather than
            derived from the acquisition, and does not match the data.
        """,
        """
            PhaseEncodingDirection was copied from another acquisition with the
            opposite polarity, which is easy in a pair of opposed-polarity scans.
        """,
        """
            The image orientation differs from what the converter assumed when it
            translated the scanner's axis convention.
        """,
    ],
    resolution="""
        Establish which acquisition each file really is by returning to the
        source data or the protocol, then make the filename and the sidecar
        agree with it. In an opposed-polarity pair, confirm the two files are not
        swapped, since that is the usual cause and it makes both files wrong at
        once.
    """,
)


# ============================================================
# Functional timing: mutually exclusive and dependent fields
#
# BIDS offers more than one way to describe when volumes were acquired. The ways
# are alternatives, not ingredients, so stating two of them at once leaves the
# timing ambiguous rather than better specified.
# ============================================================

def _mutually_exclusive(code, field_a, field_b, guidance) -> None:
    add(
        code,
        description=f"""
            {field_a} and {field_b} are both present in the same sidecar. BIDS
            treats them as mutually exclusive ways of describing the same
            acquisition timing, so only one may be given.
        """,
        interpretation=f"""
            This is an error. It is not that one of the two values is wrong; it
            is that giving both leaves the file describing its timing twice, with
            no rule saying which description wins.
        """,
        why=f"""
            Downstream software has to choose one. Different tools choose
            differently, so the same dataset yields different timing depending on
            what reads it. Removing the redundant field is what makes the timing
            unambiguous.
        """,
        causes=[
            f"""
                A conversion wrote every timing field it could derive from the
                source data, without applying the standard's exclusivity rules.
            """,
            f"""
                A field was added by hand to an existing sidecar that already
                described its timing another way.
            """,
            f"""
                A sidecar was merged from two sources, each using a different
                convention.
            """,
        ],
        resolution=f"""
            {guidance} Decide which description matches the acquisition and delete
            the other field from the sidecar. Do not leave both with the intention
            of being thorough: in this case, more metadata is less information.
        """,
    )


_mutually_exclusive(
    "VOLUME_TIMING_AND_REPETITION_TIME_MUTUALLY_EXCLUSIVE",
    "VolumeTiming", "RepetitionTime",
    """
        RepetitionTime describes a regular acquisition where volumes are evenly
        spaced, and is the right choice for most functional runs. VolumeTiming
        lists the time of every volume individually and exists for sparse or
        irregular designs where no single interval applies.
    """,
)
_mutually_exclusive(
    "VOLUME_TIMING_AND_DELAY_TIME_MUTUALLY_EXCLUSIVE",
    "VolumeTiming", "DelayTime",
    """
        DelayTime describes the gap between the end of one volume's acquisition
        and the start of the next within a regular repetition time, so it belongs
        with RepetitionTime rather than with VolumeTiming. When volume times are
        listed individually, the delays are already implied by the list.
    """,
)
_mutually_exclusive(
    "REPETITION_TIME_AND_ACQUISITION_DURATION_MUTUALLY_EXCLUSIVE",
    "RepetitionTime", "FrameAcquisitionDuration",
    """
        RepetitionTime is the interval between the starts of successive volumes.
        FrameAcquisitionDuration is how long acquiring one volume takes, which is
        meaningful alongside VolumeTiming rather than alongside RepetitionTime.
    """,
)

add(
    "VOLUME_TIMING_MISSING_ACQUISITION_DURATION",
    description="""
        VolumeTiming is present, but neither FrameAcquisitionDuration nor
        SliceTiming is given. VolumeTiming states when each volume started and
        needs one of those two to say how long each volume took.
    """,
    interpretation="""
        This is an error of incompleteness rather than contradiction. VolumeTiming
        alone describes only the start of each volume.
    """,
    why="""
        In a sparse design, the gap between volumes is deliberate and the
        acquisition occupies only part of it. Without a duration, software cannot
        tell how much of each interval contains data, which is exactly the
        information a sparse design exists to control.
    """,
    causes=[
        """
            A sparse or clustered-volume acquisition was described with
            VolumeTiming, and the companion field was not added.
        """,
        """
            RepetitionTime was replaced with VolumeTiming during a correction,
            and the duration that RepetitionTime had implied was not supplied.
        """,
    ],
    resolution="""
        Add FrameAcquisitionDuration, giving how long the acquisition of a single
        volume takes, or add SliceTiming, which gives the acquisition time of each
        slice and therefore implies the duration. Take the value from the
        acquisition protocol. If the run is in fact evenly spaced, the simpler
        description is RepetitionTime alone, without VolumeTiming.
    """,
)

add(
    "VOLUME_TIMING_NOT_MONOTONICALLY_INCREASING",
    description="""
        The values in VolumeTiming are not in increasing order. VolumeTiming lists
        the acquisition time of each volume, so the times must increase.
    """,
    interpretation="""
        This is an error. Volumes were acquired one after another, so a list of
        their times that goes backwards, or repeats, cannot describe a real
        acquisition.
    """,
    why="""
        The order of VolumeTiming establishes which entry belongs to which volume.
        A list that is out of order either mispairs times with volumes or contains
        wrong values, and both corrupt any analysis that models time.
    """,
    causes=[
        "The list was assembled from values sorted by something other than time.",
        "Duplicate entries, often from concatenating two runs.",
        "A negative or zero value inserted as a placeholder.",
        "Volumes were removed from the middle of the list without their times being removed.",
    ],
    resolution="""
        Inspect the array and find where it stops increasing, which usually
        identifies the cause immediately. Verify the times against the acquisition
        log rather than simply sorting the list, since sorting produces an
        increasing sequence that may still pair the wrong time with each volume.
    """,
)

add(
    "BOLUS_CUT_OFF_DELAY_TIME_NOT_MONOTONICALLY_INCREASING",
    description="""
        The values in BolusCutOffDelayTime are not in increasing order. When
        several cut-off delay times are given, they describe successive points in
        time and must increase.
    """,
    interpretation="""
        This is an error. The array describes an ordered sequence, so its order
        carries meaning.
    """,
    why="""
        Arterial spin labelling quantification depends on the timing of the bolus
        cut-off. Values out of order mean the model is given a sequence that
        never occurred.
    """,
    causes=[
        "Values entered in the wrong order.",
        "Values from different acquisitions combined into one array.",
        "A duplicated or placeholder value.",
    ],
    resolution="""
        Check the values against the acquisition protocol and put them in the
        order they occurred. Confirm they are in seconds, since a mixture of units
        within one array can also produce a sequence that is not increasing.
    """,
)

add(
    "SLICETIMING_VALUES_GREATER_THAN_REPETITION_TIME",
    description="""
        SliceTiming contains one or more values larger than RepetitionTime. Slice
        times are measured from the start of the volume they belong to, so every
        value must fall within one repetition time.
    """,
    interpretation="""
        This is an error. A slice acquired later than the whole volume took is
        not possible, so either the slice times or the repetition time is wrong.
    """,
    why="""
        Slice timing correction shifts each slice's time series to a common
        reference using these values. Values outside the volume's own duration
        make the correction shift data by more than a whole volume, which
        misaligns the time series rather than aligning it.
    """,
    causes=[
        """
            SliceTiming is in milliseconds while RepetitionTime is in seconds.
            This is the most common cause and it makes nearly every value exceed
            the repetition time at once.
        """,
        """
            SliceTiming was copied from an acquisition with a longer repetition
            time.
        """,
        """
            Slice times were measured from the start of the run rather than the
            start of each volume, so they grow throughout the acquisition.
        """,
    ],
    resolution="""
        Compare the largest SliceTiming value with RepetitionTime. If it is close
        to 1000 times too large, the array is in milliseconds and needs dividing
        by 1000. If the values increase steadily beyond the first volume, they are
        measured from the start of the run and must be re-expressed relative to
        each volume. Otherwise check whether RepetitionTime itself is wrong.
    """,
)

add(
    "REPETITION_TIME_MISMATCH",
    description="""
        The repetition time recorded in the image header does not match the
        RepetitionTime in the JSON sidecar.
    """,
    interpretation="""
        This is an error. The same quantity is stated in two places and the two
        disagree, so at least one is wrong.
    """,
    why="""
        Tools differ in which of the two they read. When they disagree, the
        timing used in an analysis depends on which tool ran, which makes results
        irreproducible in a way that leaves no trace in the outputs.
    """,
    causes=[
        """
            The sidecar was edited to correct the repetition time while the image
            header was left unchanged, or the other way round.
        """,
        """
            The header stores the value in different units, so the same
            acquisition reads as two different numbers.
        """,
        "The sidecar belongs to a different run with a similar name.",
        """
            A preprocessing step resampled the time series and updated the header
            without updating the sidecar.
        """,
    ],
    resolution="""
        Establish the true repetition time from the acquisition protocol rather
        than trusting either file, then make both agree with it. Note that the
        NIfTI header stores this value in its own field, which must be updated
        with a NIfTI toolkit; editing the sidecar alone leaves the disagreement in
        place.
    """,
)

add(
    "DEPRECATED_ACQUISITION_DURATION",
    description="""
        The sidecar uses AcquisitionDuration, which has been replaced by
        FrameAcquisitionDuration.
    """,
    interpretation="""
        This is a warning about a name that is no longer current. The value is
        probably correct; the field it sits in has been renamed.
    """,
    why="""
        Tools written against the current standard look for the new name and will
        not find the value under the old one, so metadata that is present appears
        missing.
    """,
    causes=[
        "The sidecar was written by an older tool, or against an older version of the standard.",
        "The field was copied from an older dataset or an older template.",
    ],
    resolution="""
        Rename the field to FrameAcquisitionDuration, keeping the value. Check
        whether the same sidecar carries other deprecated names, since a sidecar
        written against an older version usually has more than one.
    """,
)

add(
    "PHASE_SUFFIX_DEPRECATED",
    description="""
        The phase suffix is deprecated. Phase information that accompanies
        magnitude data is now expressed with the part entity instead.
    """,
    interpretation="""
        This is a warning. Files using the old suffix are still understood, but
        the current way to express the same thing is part-phase alongside
        part-mag.
    """,
    why="""
        The part entity ties a phase image to its magnitude counterpart
        explicitly, which the separate suffix did not do. Tools written against
        the current standard look for the entity, so a file using the old suffix
        may not be paired with its magnitude image.
    """,
    causes=[
        "The dataset was converted with a tool written against an earlier version of the standard.",
        "The naming was carried over from an older dataset.",
    ],
    resolution="""
        Rename the phase file to use the part-phase entity with the same suffix as
        its magnitude counterpart, and add part-mag to the magnitude file so the
        pair is explicit. Both files should otherwise carry identical entities.
        Remember to rename the JSON sidecars alongside the images.
    """,
)


# ============================================================
# Fieldmaps and distortion correction
# ============================================================

add(
    "FIELDMAP_WITHOUT_MAGNITUDE_FILE",
    description="""
        A fieldmap image has no associated magnitude image.
    """,
    interpretation="""
        This is an error. A fieldmap describes the field offset at each voxel but
        carries no anatomical contrast, so on its own it cannot be aligned to the
        images it is meant to correct.
    """,
    why="""
        Correction requires registering the fieldmap to the distorted data. That
        registration is driven by the magnitude image, which shows anatomy. Without
        it, the fieldmap cannot be placed in the right space and is unusable.
    """,
    causes=[
        "The magnitude image was not converted, often because the source series was not recognised.",
        "The magnitude image was converted but named so that it does not pair with the fieldmap.",
        "Only part of the fieldmap acquisition was copied into the dataset.",
    ],
    resolution="""
        Look in the source data for the magnitude series belonging to this
        fieldmap and convert it. The magnitude file must sit in the same fmap
        directory and carry the same entities as the fieldmap, differing only in
        suffix, or the two will not be recognised as a pair.
    """,
)

add(
    "MISSING_MAGNITUDE1_FILE",
    description="""
        A phasediff image has no associated magnitude1 image.
    """,
    interpretation="""
        This is a warning rather than an error, but the practical consequence is
        similar: a phase difference map without its magnitude image is difficult
        to use for correction.
    """,
    why="""
        The magnitude image provides the anatomical contrast used to register the
        fieldmap to the data being corrected, and is also what masking is
        typically derived from. Phase data alone is noisy outside the head and
        hard to align.
    """,
    causes=[
        "The magnitude series was not converted alongside the phase series.",
        "The magnitude image is present but named with different entities, so it does not pair.",
        "Only the phase output of the conversion was copied into the dataset.",
    ],
    resolution="""
        Convert the magnitude series from the source data and place it in the
        same fmap directory with the same entities as the phasediff file. Confirm
        that the phasediff sidecar's EchoTime1 and EchoTime2 correspond to the
        magnitude images.
    """,
)

add(
    "ECHOTIME1_2_DIFFERENCE_UNREASONABLE",
    description="""
        The difference between EchoTime2 and EchoTime1 in a phasediff sidecar
        falls outside the range 0.0001 to 0.01 seconds.
    """,
    interpretation="""
        This is an error. The echo time difference is what converts a phase
        difference into a field offset, so an implausible difference means the
        conversion factor is wrong.
    """,
    why="""
        Field offset is computed by dividing the phase difference by the echo
        time difference. An error in that difference scales the entire fieldmap,
        so the correction is applied at the wrong strength everywhere, in a way
        that looks like a plausible fieldmap.
    """,
    causes=[
        """
            The echo times are in milliseconds rather than seconds, which makes
            the difference 1000 times too large.
        """,
        """
            EchoTime1 and EchoTime2 are swapped, giving a negative difference.
        """,
        """
            The two values are identical, usually because both were copied from
            the same source field.
        """,
        """
            The values were taken from a different acquisition than the one the
            phase difference came from.
        """,
    ],
    resolution="""
        Check both values against the acquisition protocol. If the difference is
        around 1000 times too large, the values are in milliseconds and must be
        divided by 1000. If it is negative, the two are swapped. If it is zero,
        both were filled from the same source and the second must be found. The
        difference is typically a few milliseconds, that is a few thousandths of a
        second.
    """,
)

add(
    "TOTAL_READOUT_TIME_MUST_DEFINE",
    description="""
        The sidecar does not carry enough information to determine the total
        readout time. Either TotalReadoutTime must be given directly, or
        EffectiveEchoSpacing must be given so it can be derived.
    """,
    interpretation="""
        This is an error of missing metadata. It is not that a value is wrong, but
        that neither of the two accepted ways of stating the readout time is
        present.
    """,
    why="""
        Readout time determines how far susceptibility distortion displaces the
        image along the phase-encoding axis. Without it, distortion correction
        cannot compute the size of the correction, only its direction.
    """,
    causes=[
        """
            The conversion did not derive the value, which happens when the
            source data does not expose the parameters it is computed from.
        """,
        "The field was removed during anonymisation or sidecar editing.",
        "The sidecar was written from a template that omitted it.",
    ],
    resolution="""
        Add either TotalReadoutTime or EffectiveEchoSpacing to the sidecar, in
        seconds. Both can be derived from the acquisition parameters: the echo
        spacing, the number of phase-encoding lines and the parallel imaging
        acceleration factor. These are available from the scanner protocol, and
        many conversion tools will compute them if given the original source
        files rather than already-converted images.
    """,
)

add(
    "EFFECTIVEECHOSPACING_LARGER_THAN_TOTALREADOUTTIME",
    description="""
        EffectiveEchoSpacing is larger than TotalReadoutTime. The echo spacing is
        the interval between successive phase-encoding lines, and the total
        readout time spans all of them, so the spacing must be the smaller of the
        two.
    """,
    interpretation="""
        This is an error. The relationship between the two values is fixed by
        what they mean, so if it is violated at least one is wrong.
    """,
    why="""
        Both quantities feed distortion correction. If they are inconsistent, the
        computed displacement field is wrong, and because the two are used at
        different stages by different tools the error can appear at any point.
    """,
    causes=[
        "The two values are expressed in different units, typically one in seconds and one in milliseconds.",
        "The two values were swapped.",
        "One of the two was taken from a different acquisition.",
    ],
    resolution="""
        Recompute both from the acquisition parameters rather than adjusting one
        to fit the other. Total readout time is approximately the echo spacing
        multiplied by the number of phase-encoding lines acquired, so comparing
        that product against the stated readout time usually reveals which value
        is wrong.
    """,
)

add(
    "EFFECTIVEECHOSPACING_TOO_LARGE",
    description="""
        EffectiveEchoSpacing has an abnormally high value for an echo-planar
        acquisition.
    """,
    interpretation="""
        This is an error. Effective echo spacing is normally well under a
        millisecond, so a large value indicates a unit problem or a value from
        elsewhere.
    """,
    why="""
        Echo spacing scales the distortion correction. A value orders of magnitude
        too large produces a correction of the wrong size, which distorts the
        image rather than repairing it.
    """,
    causes=[
        "The value is in milliseconds or microseconds rather than seconds.",
        "The nominal echo spacing was used without dividing by the parallel imaging acceleration factor.",
        "The value was taken from a different sequence.",
    ],
    resolution="""
        Express the value in seconds. Effective echo spacing accounts for parallel
        imaging, so it is the nominal echo spacing divided by the acceleration
        factor, and is typically a few hundred microseconds, that is a few ten
        thousandths of a second. Recompute it from the protocol rather than
        rescaling the existing number by a guessed factor.
    """,
)

add(
    "EPI_WITH_BVALS_NEEDS_SMALL_BVALS",
    description="""
        An EPI fieldmap has associated b-values, but none of them is small. A
        fieldmap acquisition used for distortion correction should include at
        least one low b-value volume, below 100.
    """,
    interpretation="""
        This is an error. The presence of b-values means the acquisition is
        diffusion-weighted, and distortion correction needs an essentially
        undiffusion-weighted volume to work from.
    """,
    why="""
        Registration between the fieldmap and the data being corrected relies on
        anatomical contrast. Strongly diffusion-weighted volumes have little of
        it and low signal-to-noise, so a fieldmap consisting only of high b-value
        volumes cannot be registered reliably.
    """,
    causes=[
        "Only diffusion-weighted volumes were included when the fieldmap was assembled.",
        "The b-zero volumes were separated into a different file.",
        "The b-value file belongs to a different acquisition.",
    ],
    resolution="""
        Include the low b-value volumes in the fieldmap acquisition, or use the
        acquisition that contains them. Check that the .bval file really
        corresponds to this file, since a mismatched b-value file is a common
        cause.
    """,
)


# ============================================================
# Diffusion gradient tables
# ============================================================

add(
    "DWI_MISSING_BVAL",
    description="""
        A diffusion-weighted image has no accompanying .bval file. The .bval file
        lists the diffusion weighting applied to each volume.
    """,
    interpretation="""
        This is an error. Without b-values, the volumes are just images with no
        record of how they were diffusion-weighted.
    """,
    why="""
        Every diffusion model, from a tensor fit upward, needs to know each
        volume's b-value. Without the file, no diffusion analysis is possible,
        and the data cannot be interpreted later even though the images are
        intact.
    """,
    causes=[
        """
            The conversion did not write the gradient files, which happens when it
            is run on already-converted images rather than the original source
            data.
        """,
        """
            The .bval file exists but does not share the image's exact name, so
            it is not recognised as belonging to it.
        """,
        "Only the image was copied into the dataset.",
        """
            The file is present at a higher level in the hierarchy intending to
            be inherited, but its entities do not match this image.
        """,
    ],
    resolution="""
        Find the .bval file produced by the original conversion and place it
        beside the image with exactly the same name and the .bval extension. If it
        was never produced, re-convert from the original source data, which
        carries the gradient information. A .bval can be inherited from a higher
        level when several runs share one gradient scheme, but only if its
        entities match.
    """,
)

add(
    "DWI_MISSING_BVEC",
    description="""
        A diffusion-weighted image has no accompanying .bvec file. The .bvec file
        gives the direction of the diffusion gradient for each volume.
    """,
    interpretation="""
        This is an error, and it matters more than it first appears: gradient
        directions are tied to the image's orientation, so they cannot be
        reconstructed after the fact from a protocol document alone.
    """,
    why="""
        Directional diffusion analysis, including tensor fitting and tractography,
        is impossible without gradient directions. Unlike b-values, which are
        often documented elsewhere, the directions depend on how the image was
        stored and must come from the conversion.
    """,
    causes=[
        "The conversion did not write the gradient files, typically because it ran on converted images rather than source data.",
        "The .bvec file does not share the image's exact name.",
        "Only the image and the .bval were copied.",
    ],
    resolution="""
        Recover the .bvec from the original conversion, or re-convert from the
        source data. Do not reconstruct the directions by hand from a protocol
        table: the convention relating gradient directions to image axes depends
        on the conversion, and a table applied under the wrong convention produces
        tractography that is confidently wrong.
    """,
)

add(
    "BVAL_MULTIPLE_ROWS",
    description="""
        A .bval file contains more than one row. It must contain exactly one row,
        holding one b-value per volume.
    """,
    interpretation="""
        This is an error about file format. The b-value file is a single row of
        numbers separated by spaces.
    """,
    why="""
        Readers expect a specific shape and will either fail or misread a file
        with extra rows, potentially assigning b-values to the wrong volumes.
    """,
    causes=[
        """
            The file was transposed, so one value appears per line. This is the
            most common form and is easy to spot by opening the file.
        """,
        "Two acquisitions' b-value files were concatenated.",
        "A trailing blank line or a header line was added by an editor.",
        "The .bvec content was written into the .bval file, which has three rows.",
    ],
    resolution="""
        Open the file. It should be a single line with one number per volume,
        separated by spaces. If the values run down the file instead of across,
        transpose it. If the file has three rows of directions, it is a .bvec and
        the two files have been swapped.
    """,
)

add(
    "BVEC_NUMBER_ROWS",
    description="""
        A .bvec file does not contain exactly three rows. It must have three, one
        for each of the x, y and z components of the gradient direction.
    """,
    interpretation="""
        This is an error about file format. Each row holds one component across
        all volumes, so there are three rows and as many columns as volumes.
    """,
    why="""
        Gradient directions are three-dimensional vectors. A file with the wrong
        number of rows cannot be read as directions at all, and a transposed file
        can be misread as a much smaller number of volumes.
    """,
    causes=[
        """
            The file is transposed, with one direction per line rather than one
            component per line. This is the usual cause.
        """,
        "The .bval content was written into the .bvec file, which has one row.",
        "Extra blank or header lines.",
        "Two files concatenated.",
    ],
    resolution="""
        Open the file and check its shape. Three rows, each with one entry per
        volume, is correct. If it has as many rows as the image has volumes, it is
        transposed. If it has one row, the .bval and .bvec have been swapped.
    """,
)

add(
    "BVEC_ROW_LENGTH",
    description="""
        The rows of a .bvec file do not all contain the same number of values.
    """,
    interpretation="""
        This is an error. The three rows are the x, y and z components of the same
        set of directions, so they must be the same length.
    """,
    why="""
        Unequal rows mean the three components cannot be assembled into vectors.
        Readers either fail or silently pad, and padded directions point
        somewhere arbitrary.
    """,
    causes=[
        "A value was lost or duplicated while the file was edited.",
        "Inconsistent separators, such as a mixture of tabs and spaces, or two spaces between values.",
        "A line wrapped when the file was copied through an editor or email.",
        "Rows taken from different acquisitions.",
    ],
    resolution="""
        Count the values in each row; the shortest or longest usually identifies
        where the problem is. Confirm that values are separated by single spaces
        and that no row has wrapped. All three rows must have exactly as many
        values as the image has volumes.
    """,
)

add(
    "B_FILE",
    description="""
        A .bval or .bvec file is not formatted as BIDS requires. These files must
        be single-space delimited and contain only numbers.
    """,
    interpretation="""
        This is an error about the file's contents rather than its shape. Something
        in it is not a number, or the separators are not single spaces.
    """,
    why="""
        These files are parsed by splitting on whitespace and converting to
        numbers. Anything else in the file stops the parse, and the diffusion
        directions become unavailable even though they are visible in the file.
    """,
    causes=[
        "Tabs or multiple spaces used as separators instead of single spaces.",
        "A header line naming the columns.",
        "Comma separators, typically from a spreadsheet export.",
        """
            Non-numeric placeholders such as n/a, NaN or empty entries where a
            number belongs.
        """,
        "Trailing whitespace or a stray character at the end of a line.",
    ],
    resolution="""
        Open the file and confirm it contains only numbers separated by single
        spaces, with no header and no trailing characters. Note that these files
        are not TSV files despite sitting among them, so a spreadsheet is the wrong
        tool for editing them.
    """,
)

add(
    "MALFORMED_BVAL",
    description="""
        The contents of a .bval file could not be interpreted at all.
    """,
    interpretation="""
        This is an error and a more severe form of a formatting problem: the file
        is not merely irregular, it cannot be read as b-values.
    """,
    why="""
        Without b-values, the diffusion weighting of each volume is unknown and no
        diffusion analysis can proceed.
    """,
    causes=[
        "The file is empty or contains only whitespace.",
        "The file holds text rather than numbers, for example an error message written by a failed conversion.",
        "The file is binary, or is a different file that was renamed.",
        "The conversion wrote a partial file.",
    ],
    resolution="""
        Open the file and look at what is actually in it, which usually explains
        the failure immediately. Re-generate it from the original source data by
        converting again. If the source is unavailable, the b-values may be
        recoverable from the acquisition protocol, but the gradient directions in
        the .bvec generally are not.
    """,
)

add(
    "MALFORMED_BVEC",
    description="""
        The contents of a .bvec file could not be interpreted at all.
    """,
    interpretation="""
        This is an error. The file cannot be read as gradient directions.
    """,
    why="""
        Gradient directions cannot be reconstructed from documentation, because
        they depend on the orientation convention used when the image was
        written. A lost .bvec usually means re-converting from source is the only
        reliable route.
    """,
    causes=[
        "The file is empty or contains only whitespace.",
        "The file holds text rather than numbers.",
        "The file is binary, or a different file that was renamed.",
        "The conversion wrote a partial file.",
    ],
    resolution="""
        Look at the file's contents to confirm the diagnosis, then re-convert from
        the original source data. Avoid substituting a gradient table from
        documentation unless you can confirm which orientation convention it uses,
        since directions applied under the wrong convention produce plausible but
        incorrect tractography.
    """,
)

add(
    "VOLUME_COUNT_MISMATCH",
    description="""
        The number of volumes in a diffusion image does not match the number of
        entries in its .bval and .bvec files.
    """,
    interpretation="""
        This is an error. Each volume needs exactly one b-value and one gradient
        direction, so the three counts must agree.
    """,
    why="""
        If the counts differ, the pairing between volumes and gradients is wrong
        from the point of the discrepancy onward. Every volume after it is
        analysed with another volume's gradient, which corrupts the fit without
        producing any obvious failure.
    """,
    causes=[
        """
            Volumes were removed from the image, for example corrupted ones
            dropped during quality control, without the corresponding entries
            being removed from the gradient files.
        """,
        """
            The acquisition was stopped early, so fewer volumes were saved than
            the gradient table describes.
        """,
        """
            The gradient files belong to a different run, which is easy when
            several diffusion runs share a scheme.
        """,
        "Two runs were concatenated but only one gradient table was carried over.",
    ],
    resolution="""
        Count the volumes in the image, the values in the .bval and the columns in
        the .bvec. If volumes were dropped, remove the same positions from both
        gradient files, which requires knowing which volumes were removed. If the
        gradient files belong to another run, find the correct ones. Do not trim
        the gradient files from the end to match the count unless you know the
        missing volumes were the last ones.
    """,
)


# ============================================================
# Dataset-level structure and descriptive files
# ============================================================

add(
    "SUBJECT_FOLDERS",
    description="""
        No directories named sub-<label> were found at the root of the dataset.
        BIDS organises data into one directory per participant at the top level.
    """,
    interpretation="""
        This is a warning, but in practice it usually means the validator is not
        looking at a BIDS dataset root at all.
    """,
    why="""
        Subject directories are how every BIDS tool enumerates participants. With
        none present, the dataset has no data any tool can find, whatever it
        contains.
    """,
    causes=[
        """
            The validator was pointed at a parent directory, or at one level
            inside the dataset, rather than at the dataset root.
        """,
        """
            The subject directories are named without the sub- prefix, for
            example using bare identifiers or names.
        """,
        """
            Data is grouped by session or by datatype at the top level, with
            subjects nested inside, which is the reverse of the BIDS hierarchy.
        """,
        "The dataset holds only source data that has not been converted yet.",
    ],
    resolution="""
        Confirm you are pointing at the directory that contains
        dataset_description.json. If you are, check the naming of the participant
        directories: each must be sub- followed by a label of letters and digits
        only, with no underscores, hyphens or spaces. If the hierarchy is grouped
        by session or datatype first, it has to be reorganised so that subject is
        the outermost level.
    """,
)

add(
    "NOSUBJECT_FOLDERS",
    description="""
        Subject directories were found at the root of a directory that is not
        expected to contain them, for example a derivatives or sourcedata
        directory being treated as a study root.
    """,
    interpretation="""
        This is a warning about where subject directories appear relative to the
        dataset they belong to.
    """,
    why="""
        The location of subject directories determines which dataset they belong
        to. Misplaced, the same data can be counted twice, or a derivative
        dataset can be mistaken for raw data.
    """,
    causes=[
        "A derivatives dataset placed without its own dataset_description.json.",
        "Nested datasets where the boundary between them is not marked.",
        "Subject directories left at a level that the standard reserves for something else.",
    ],
    resolution="""
        Check the directory layout against the structure the standard defines for
        this kind of dataset. A derivatives dataset needs its own
        dataset_description.json declaring it as a derivative; without one, its
        subject directories are read as part of the parent dataset.
    """,
    confidence="medium",
)

add(
    "PARTICIPANT_ID_MISMATCH",
    description="""
        The subject directories present in the dataset do not match the values in
        the participant_id column of participants.tsv.
    """,
    interpretation="""
        This is an error. participants.tsv is the dataset's own list of who is in
        it, and it disagrees with what is actually on disk. The mismatch can run
        either way: a listed participant with no directory, or a directory with no
        row.
    """,
    why="""
        participants.tsv is where demographic and group information lives. If the
        identifiers do not line up, that information is attached to the wrong
        person or to nobody, which can silently misassign group membership in an
        analysis. It is also a common symptom of a partially copied dataset.
    """,
    causes=[
        """
            A participant was excluded and their directory removed, without the
            participants.tsv row being removed, or the reverse.
        """,
        """
            The participant_id values omit the sub- prefix. The column must
            contain the full directory name, such as sub-01, not the bare label.
        """,
        """
            Zero-padding differs between the table and the directories, so sub-1
            and sub-01 do not match.
        """,
        """
            Only part of the dataset was copied, so directories are missing for
            participants that are legitimately listed.
        """,
        """
            Trailing whitespace in the column, which is invisible in a
            spreadsheet but makes the strings unequal.
        """,
    ],
    resolution="""
        List the subject directories and compare them against the participant_id
        column. Check the prefix and the zero-padding first, since those account
        for most mismatches and affect every row at once. Then reconcile the
        genuine differences: add rows for participants that exist, remove rows for
        participants that do not, and confirm no rows were lost when the file was
        last edited.
    """,
    examples=[
        """
            participants.tsv lists 01, 02 and 03 while the directories are named
            sub-01, sub-02 and sub-03. The column needs the sub- prefix on every
            row.
        """,
    ],
)

add(
    "PHENOTYPE_SUBJECTS_MISSING",
    description="""
        A file in the phenotype/ directory lists participants that do not appear
        in the participant_id column of participants.tsv.
    """,
    interpretation="""
        This is an error. Phenotype tables describe participants in the dataset,
        so every participant they mention must be one of them.
    """,
    why="""
        Phenotype data that refers to unknown participants cannot be joined to the
        imaging data. It usually means either that participants were removed
        without the phenotype files being updated, or that the table came from a
        larger cohort than the dataset contains.
    """,
    causes=[
        """
            The phenotype table was exported for the whole study while the
            dataset holds a subset of participants.
        """,
        "Participants were excluded from the dataset after the phenotype file was written.",
        "Identifiers are formatted differently between the two files, for example with or without the sub- prefix.",
    ],
    resolution="""
        Compare the identifiers in the phenotype file against participants.tsv,
        checking the prefix and padding first. Remove rows for participants who
        are not in the dataset, or add the missing participants to
        participants.tsv if they should be there. Take care not to include
        phenotype data for people whose imaging data was deliberately excluded.
    """,
)

add(
    "SCANS_FILENAME_NOT_MATCH_DATASET",
    description="""
        The filename column of a scans.tsv lists files that are not present in the
        dataset.
    """,
    interpretation="""
        This is an error. A scans table records the recordings acquired for a
        subject or session, so its rows must correspond to files that exist.
    """,
    why="""
        Scans tables carry per-recording information such as acquisition time,
        which is how sessions are ordered and how recordings are related in time.
        Rows pointing at absent files mean that information is attached to nothing,
        and usually mean a recording was renamed or removed without the table
        following.
    """,
    causes=[
        """
            Files were renamed, for example while correcting entities, and the
            scans table was not updated.
        """,
        "Recordings were removed from the dataset while their rows remained.",
        """
            The paths in the filename column are written relative to the wrong
            location. They must be relative to the directory the scans file sits
            in, starting with the datatype directory.
        """,
        "Backslashes used as path separators, which do not match on any platform.",
    ],
    resolution="""
        Check the form of the paths first: each should begin with the datatype
        directory and use forward slashes, for example anat/sub-01_T1w.nii.gz.
        Then reconcile the rows against the files present, updating names that
        changed and removing rows for recordings that are gone. Renaming a
        recording always means editing its scans row too.
    """,
)

add(
    "SAMPLES_TSV_MISSING",
    description="""
        The dataset contains microscopy data but has no /samples.tsv file at its
        root. That file is required whenever samples are present.
    """,
    interpretation="""
        This is an error. samples.tsv is where each sample is declared and tied to
        the participant it came from.
    """,
    why="""
        In microscopy, the sample is a level of organisation between participant
        and image. Without samples.tsv there is no record of what each sample is
        or whom it came from, which makes the images uninterpretable.
    """,
    causes=[
        "The file was not created when the dataset was assembled.",
        "The file exists at the wrong level, for example inside a subject directory rather than at the dataset root.",
        "The file has a different name or extension.",
    ],
    resolution="""
        Create /samples.tsv at the dataset root with the required columns:
        sample_id, participant_id and sample_type. Each sample referenced by a
        sample entity in any filename must have a row, and each participant_id
        must match a subject directory.
    """,
)

add(
    "README_FILE_MISSING",
    description="""
        The dataset has no README file at its root.
    """,
    interpretation="""
        This is a warning. The dataset is valid without one, but the standard
        recommends a README and most repositories require one before accepting a
        dataset.
    """,
    why="""
        A README is the only place that explains the study in prose: what was
        acquired, why, what the task was, what the participants did, what is
        unusual about the data. None of that is expressible in metadata fields,
        and without it a dataset is very hard to reuse even when it is perfectly
        structured.
    """,
    causes=[
        "The file was never written.",
        "It is named differently, for example readme.txt in lower case or notes.md.",
        "It sits inside a subdirectory rather than at the dataset root.",
    ],
    resolution="""
        Add a file named README at the dataset root. It may carry no extension or
        one of .md, .rst or .txt. Describe the study, the participants, the tasks
        and anything a person reusing the data would otherwise have to guess.
        Length is less important than covering what the structured metadata cannot
        say.
    """,
)

add(
    "MULTIPLE_README_FILES",
    description="""
        More than one README file is present at the dataset root, differing only
        in extension.
    """,
    interpretation="""
        This is an error. The dataset must have exactly one README, so that there
        is no question which one describes it.
    """,
    why="""
        Two READMEs are two descriptions of the same dataset with no rule saying
        which is current. In practice one is usually an abandoned draft, and a
        reader has no way to tell which.
    """,
    causes=[
        "A README was converted from one format to another and the original was left behind.",
        "Two people added a README in different formats.",
        "A template README was left in place alongside the real one.",
    ],
    resolution="""
        Decide which file is current, merge anything worth keeping from the other
        into it, and delete the rest. Keep exactly one file named README, with at
        most one of the permitted extensions.
    """,
)

add(
    "README_FILE_SMALL",
    description="""
        A README file is present but very short.
    """,
    interpretation="""
        This is a warning about content rather than structure. The file exists, so
        the dataset is valid; the suggestion is that it does not yet say much.
    """,
    why="""
        A README is the main route by which somebody who did not collect the data
        understands it. A placeholder satisfies the check while leaving the
        dataset as opaque as it was.
    """,
    causes=[
        "A placeholder was created to satisfy the requirement and never expanded.",
        "The description lives elsewhere, such as in a paper or a lab wiki, and was not brought into the dataset.",
    ],
    resolution="""
        Expand the README to cover what the structured metadata cannot: the
        purpose of the study, the participant population and how they were
        recruited, what each task involved, the scanner or recording equipment,
        anything unusual about individual sessions, and how the dataset should be
        cited. Datasets destined for a public repository benefit most, since the
        README is what a prospective reuser reads first.
    """,
)

add(
    "EMPTY_DATASET_NAME",
    description="""
        The Name field of dataset_description.json is present but contains no
        visible characters.
    """,
    interpretation="""
        This is a warning. The required field is technically there, so the
        structure is satisfied, but it carries no information.
    """,
    why="""
        The dataset name is how the dataset is identified in listings, in
        repositories and in the outputs of tools that report which dataset they
        processed. An empty name means every one of those shows a blank.
    """,
    causes=[
        "A template dataset_description.json was created and the name was never filled in.",
        "The field holds only whitespace.",
        "An automated step wrote an empty string as a placeholder.",
    ],
    resolution="""
        Set Name to a short descriptive title for the study. It should be
        recognisable on its own, since it will appear away from any other context.
    """,
)

add(
    "TOO_FEW_AUTHORS",
    description="""
        The Authors field of dataset_description.json does not appear to list
        authors individually. It should be an array with one entry per author.
    """,
    interpretation="""
        This is a warning. It usually means several names were written into a
        single string rather than as separate array entries.
    """,
    why="""
        Author lists are read programmatically for citation and attribution. A
        single string containing several names is one author as far as any
        consumer is concerned, so individual contributors are not credited and
        cannot be matched to identifiers.
    """,
    causes=[
        """
            All authors written as one comma-separated string rather than as
            separate array entries.
        """,
        "A single author entry where the study has several.",
        "The field left as a template placeholder.",
    ],
    resolution="""
        Write Authors as a JSON array with one string per author, for example
        ["Family, Given", "Family, Given"]. Use a consistent name format across
        entries.
    """,
    notes="""
        If the dataset carries a CITATION.cff file, authorship belongs there
        instead and the Authors field must be removed from
        dataset_description.json, since the two are mutually exclusive.
    """,
)

add(
    "AUTHORS_AND_CITATION_FILE_MUTUALLY_EXCLUSIVE",
    description="""
        The dataset has a CITATION.cff file and dataset_description.json also has
        an Authors field. Only one of the two may state authorship.
    """,
    interpretation="""
        This is an error. It is not that either is wrong on its own, but that two
        authoritative author lists in one dataset can disagree.
    """,
    why="""
        CITATION.cff is the richer format, carrying identifiers, affiliations and
        roles. When both are present they will drift apart, and different tools
        read different ones, so the dataset credits different people depending on
        what processes it.
    """,
    causes=[
        """
            A CITATION.cff was added to an existing dataset without removing the
            Authors field it superseded.
        """,
        "Both files were generated from a template that fills in everything it can.",
    ],
    resolution="""
        Confirm that CITATION.cff lists every author correctly, then remove the
        Authors field from dataset_description.json. Keep CITATION.cff as the
        single source, since it is the format designed for the purpose.
    """,
)

add(
    "SINGLE_SOURCE_CITATION_FIELDS",
    description="""
        The dataset has a CITATION.cff file, and dataset_description.json also
        carries HowToAcknowledge, License or ReferencesAndLinks. Those fields
        belong in one place only.
    """,
    interpretation="""
        This is a warning with the same logic as the authorship rule: citation
        information should have a single source so the two cannot diverge.
    """,
    why="""
        Licence terms in particular must be unambiguous. Two statements of a
        licence in one dataset is a problem for anyone deciding whether they may
        reuse the data.
    """,
    causes=[
        "A CITATION.cff was added without removing the corresponding fields from dataset_description.json.",
        "Both were generated independently from study records.",
    ],
    resolution="""
        Move the content into CITATION.cff if it is not already there, confirm it
        is correct, and remove HowToAcknowledge, License and ReferencesAndLinks
        from dataset_description.json. Check especially that the licence stated in
        CITATION.cff is the one you intend.
    """,
)

add(
    "UNKNOWN_BIDS_VERSION",
    description="""
        The BIDSVersion field of dataset_description.json does not name a known
        BIDS release, so the validator fell back to its own default schema
        version.
    """,
    interpretation="""
        This is a warning. Validation still ran, but against a version the dataset
        did not ask for, so the results describe compliance with a different
        version of the standard than the one declared.
    """,
    why="""
        BIDS evolves. A rule that is required in one version may be recommended in
        another, and suffixes and entities are added over time. Validating against
        the wrong version can report problems that do not exist under the declared
        version, or miss ones that do.
    """,
    causes=[
        "A typo in the version string, or a missing patch component.",
        "A development or pre-release version string that names no published release.",
        "A version newer than the validator knows about.",
        "The field left as a placeholder.",
    ],
    resolution="""
        Set BIDSVersion to a published BIDS release that the dataset actually
        conforms to, written as a plain version number. If the dataset was built
        against a development version of the standard, decide which released
        version to declare before sharing it, since repositories and tools
        generally expect a stable one. If the version is simply newer than the
        validator, update the validator.
    """,
)

add(
    "MISSING_SESSION",
    description="""
        Not all subjects in the dataset contain the same sessions.
    """,
    interpretation="""
        This is a warning, and frequently it describes reality rather than a
        defect. Longitudinal studies routinely have participants who missed a
        session.
    """,
    why="""
        Uneven sessions across participants are worth noticing because they are
        equally consistent with a genuine dropout and with data that was never
        copied across. The validator cannot distinguish the two, so it reports
        the pattern and leaves the judgement to you.
    """,
    causes=[
        "A participant genuinely did not attend a session, which is normal in longitudinal designs.",
        "A session's data was not copied into the dataset, or was copied to the wrong participant.",
        "Session labels are inconsistent between participants, for example ses-1 for one and ses-01 for another.",
        "A session was excluded for quality reasons and its directory removed.",
    ],
    resolution="""
        Compare the sessions present for each participant against your own
        records. Check the labelling first, since inconsistent labels make
        identical sessions look different. If a participant genuinely lacks a
        session, no change is needed and the warning is expected; document the
        absence in the README or in a sessions table so that a later reader knows
        it was intended.
    """,
)


# ============================================================
# Events and stimuli
# ============================================================

add(
    "EVENTS_TSV_MISSING",
    description="""
        A scan with a task entity has no corresponding events.tsv file.
    """,
    interpretation="""
        This is a warning, and whether it matters depends entirely on the task.
        For a task with discrete trials it is a real omission. For a resting-state
        run, which has a task entity by convention but no events, it is expected
        and can be ignored.
    """,
    why="""
        An events file is what makes a task run analysable: it says what happened
        and when. Without it, a task dataset records that something was done to
        the participant but not what. Timing information is also difficult to
        reconstruct later, since it lives in the stimulus software's logs rather
        than in the imaging data.
    """,
    causes=[
        """
            The run is resting state, conventionally named with a task entity such
            as task-rest, and genuinely has no events. This is the most common
            case and needs no action.
        """,
        "Stimulus presentation logs were never converted into an events.tsv.",
        "The events file exists but its entities do not match the recording exactly, so it is not paired.",
        "The events file sits in the wrong directory.",
    ],
    resolution="""
        Decide first whether the run has events at all. If it is resting state,
        ignore the warning. If it has a task, convert the stimulus software's log
        into an events.tsv with at minimum the onset and duration columns, onsets
        measured in seconds from the first stored data point. If a file already
        exists, check that its entities match the recording exactly, since an
        events file is paired with its recording by name.
    """,
)

add(
    "EVENT_ONSET_ORDER",
    description="""
        The onset column of an events.tsv is not sorted in increasing order.
    """,
    interpretation="""
        This is a warning. The standard recommends events be listed in the order
        they occurred, which makes the file readable and matches what most tools
        assume.
    """,
    why="""
        Some software reads events sequentially and assumes chronological order.
        More practically, an unsorted file is hard to inspect by eye, and
        out-of-order onsets often indicate that rows from different conditions or
        different runs were concatenated without being merged properly.
    """,
    causes=[
        "The file was assembled by concatenating one block of rows per condition rather than merging by time.",
        "The file was sorted by trial type, or by another column, before being saved.",
        "Rows from two runs were combined.",
        "A correction added rows at the end rather than in place.",
    ],
    resolution="""
        Sort the rows by the onset column. Before doing so, check why they were
        out of order: if two runs were concatenated, sorting merges them into one
        timeline, which is not a fix. Onsets must be measured from the start of
        the recording the file belongs to.
    """,
)

add(
    "SUSPICIOUS_NEGATIVE_EVENT_ONSET",
    description="""
        One or more event onsets fall more than 60 seconds before the start of the
        recording, indicated by large negative values.
    """,
    interpretation="""
        This is a warning. Negative onsets are permitted by the standard, for
        events that genuinely occurred before the first stored data point, but
        values this far back are unusual.
    """,
    why="""
        Onsets are measured from the first stored data point of the recording.
        Large negative values normally mean the events were timed against a
        different clock, so every onset in the file may be offset by the same
        amount, which shifts the whole design.
    """,
    causes=[
        """
            Onsets measured from the start of the stimulus program rather than
            from the first stored data point, so the offset between the two clocks
            appears as a constant shift.
        """,
        """
            Dummy volumes were discarded from the recording without the onsets
            being re-referenced to the new start.
        """,
        "Timestamps in absolute wall-clock time rather than relative to the recording.",
        "A genuine pre-scan training period, in which case the values are correct.",
    ],
    resolution="""
        Look at whether all onsets share a similar offset, which points to a
        timing reference problem affecting the whole file rather than a few stray
        rows. Re-reference the onsets to the first stored data point of the
        recording. If events legitimately preceded the recording, the negative
        values are correct and the warning can be left.
    """,
)

add(
    "SUSPICIOUS_POSITIVE_EVENT_ONSET",
    description="""
        One or more event onsets fall more than a month after the start of the
        recording.
    """,
    interpretation="""
        This is a warning. An onset that far out is almost certainly a timestamp
        rather than an elapsed time.
    """,
    why="""
        Onsets are elapsed seconds from the start of the recording. A value of
        that magnitude is an absolute time in disguise, usually seconds since an
        epoch, which means no event in the file is placed correctly.
    """,
    causes=[
        """
            Onsets written as absolute timestamps, for example seconds since the
            Unix epoch, rather than as elapsed time.
        """,
        "Onsets in milliseconds or microseconds rather than seconds.",
        "A stray or corrupted value in one row.",
    ],
    resolution="""
        Check whether the values are timestamps by looking at their magnitude and
        at the differences between them: if the differences are plausible while
        the values are enormous, subtract the recording's start time from every
        onset. If the whole column is a fixed factor too large, correct the units.
        Onsets are always in seconds relative to the recording.
    """,
)

add(
    "SUSPICIOUSLY_LONG_EVENT_DESIGN",
    description="""
        The onset of the last event in the events file falls after the end of the
        corresponding recording.
    """,
    interpretation="""
        This is a warning. Events are supposed to describe what happened during
        the recording, so an event after it ended is either mistimed or belongs to
        a different run.
    """,
    why="""
        An event outside the recording cannot be modelled, and its presence
        suggests the events file and the recording do not correspond. The rest of
        the file's timings may be wrong in the same way.
    """,
    causes=[
        "The events file belongs to a different, longer run.",
        "The recording was truncated or stopped early while the events file describes the full protocol.",
        "Onsets in the wrong unit or measured from the wrong reference point.",
        "Events from several runs concatenated into one file.",
    ],
    resolution="""
        Compare the last onset with the recording's duration, which is the number
        of volumes multiplied by the repetition time for imaging data. If the
        events file describes a longer session, check it belongs to this run. If
        the recording stopped early, keep only the events that fall within it and
        note the truncation in the README.
    """,
)

add(
    "SUSPICIOUSLY_SHORT_EVENT_DESIGN",
    description="""
        The onset of the last event falls in the first half of the recording, so
        the second half appears to contain no events.
    """,
    interpretation="""
        This is a warning about a pattern that is sometimes correct. A design with
        a long post-task period is legitimate; more often the events file is
        truncated.
    """,
    why="""
        If the events file is incomplete, the unmodelled portion of the recording
        is treated as baseline, which biases the estimated response for every
        condition, not only the missing trials.
    """,
    causes=[
        "The events file was truncated when it was written or exported.",
        "The stimulus program crashed or was stopped part way while the recording continued.",
        "Only one of several blocks was converted into the events file.",
        "The events belong to a shorter run.",
        "The design genuinely has a long period with no events.",
    ],
    resolution="""
        Compare the last onset with the recording duration and against your
        protocol. If the design really does end early, no change is needed. If
        events are missing, recover them from the stimulus software's logs. If the
        session was interrupted, record that in the README, since the recording
        then contains a period that was never part of the experiment.
    """,
)

add(
    "STIMULUS_FILE_MISSING",
    description="""
        An events file refers to a stimulus file in its stim_file column, but that
        file is not present in the dataset.
    """,
    interpretation="""
        This is an error. The reference names a file that does not exist.
    """,
    why="""
        Stimulus files are what make an experiment reproducible: without the
        actual images, sounds or videos presented, the events file records that
        something was shown but not what. Recovering them later is often
        impossible.
    """,
    causes=[
        """
            The stimuli were not copied into the dataset. They belong in a
            /stimuli directory at the dataset root.
        """,
        """
            The paths in stim_file are written relative to the wrong location.
            They are relative to the /stimuli directory, so the column should not
            repeat stimuli/ in the path.
        """,
        "Filenames differ in case, which matters on most systems.",
        "Stimuli were omitted deliberately for copyright reasons.",
    ],
    resolution="""
        Create a /stimuli directory at the dataset root and place the stimulus
        files in it, then check that the stim_file values are paths relative to
        that directory. If the stimuli cannot be shared for licensing reasons,
        remove the stim_file column and describe the stimuli in the README,
        rather than leaving references that resolve to nothing.
    """,
)

add(
    "BEH_ONSET_DURATION",
    description="""
        A file with the beh suffix contains onset and duration columns, which are
        the defining columns of an events file.
    """,
    interpretation="""
        This is a warning. The file is shaped like an events file while being
        named as behavioural data, so tools looking for events will not find it.
    """,
    why="""
        The suffix is how tools decide what a file is. Timed events stored under
        the beh suffix are invisible to anything searching for events, so the
        timing information is present but unreachable.
    """,
    causes=[
        """
            Behavioural responses with trial timing were saved as beh when they
            describe events.
        """,
        """
            A converter wrote a single file containing both trial timings and
            summary measures.
        """,
    ],
    resolution="""
        If the rows describe timed events, rename the file to use the events
        suffix. If it holds both timed events and per-run summary measures, split
        it: the timed rows into an events file, the summaries into a beh file.
        The beh suffix is for behavioural data that is not a time series of
        events.
    """,
)


# ============================================================
# Cross-file references
#
# BIDS links files by writing one file's path inside another file's metadata.
# Those links are plain strings, so nothing stops them going stale when files are
# renamed, and nothing except the validator ever reports that they have.
# ============================================================

add(
    "INTENDED_FOR",
    description="""
        An IntendedFor field names a file that does not exist in the dataset.
        IntendedFor declares which recordings a fieldmap is meant to correct.
    """,
    interpretation="""
        This is an error. The reference is a path written as text, so it is
        correct only as long as the file it names keeps that exact name.
    """,
    why="""
        Fieldmap correction is applied by finding the fieldmaps whose IntendedFor
        names the image being corrected. A broken reference does not usually
        fail loudly: the pipeline finds no applicable fieldmap and proceeds
        without distortion correction, producing output that looks normal and is
        uncorrected.
    """,
    causes=[
        """
            Files were renamed after the fieldmap sidecar was written, for
            example while correcting entities or adding a session, and the
            references were not updated.
        """,
        """
            The path is written at the wrong level. A plain path in IntendedFor
            is relative to the subject directory and therefore starts with the
            session or datatype directory, not with sub-. A BIDS URI beginning
            bids:: is relative to the dataset root instead.
        """,
        """
            The sidecar was copied from another subject, so the paths still name
            that subject's files.
        """,
        "A typo, or a difference in case, in the path.",
        "The target recording was removed from the dataset while the reference remained.",
    ],
    resolution="""
        Compare each path in IntendedFor against the files actually present.
        Check the form of the path first, since getting the base directory wrong
        breaks every entry at once: a plain path is relative to the subject
        directory, while a path beginning with bids:: is relative to the dataset
        root. Then correct the individual names. Any renaming of recordings has
        to be accompanied by updating every IntendedFor that points at them.
    """,
    examples=[
        """
            A fieldmap declares IntendedFor as sub-01/func/sub-01_task-rest_bold.nii.gz.
            Because plain paths are relative to the subject directory, the
            leading sub-01/ is one level too many; the correct value is
            func/sub-01_task-rest_bold.nii.gz.
        """,
    ],
)

add(
    "ASSOCIATED_EMPTY_ROOM",
    description="""
        An AssociatedEmptyRoom field names a file that does not exist in the
        dataset. The field links a MEG recording to the empty-room recording used
        to characterise environmental noise.
    """,
    interpretation="""
        This is an error. As with other cross-file references, the link is a path
        written as text and breaks silently when files move.
    """,
    why="""
        Empty-room recordings are used to estimate the noise covariance for
        source reconstruction. If the link does not resolve, the noise model has
        to be estimated some other way or omitted, which changes source estimates
        without any obvious sign in the output.
    """,
    causes=[
        "The empty-room recording was not included in the dataset.",
        """
            The path is written at the wrong level. AssociatedEmptyRoom takes a
            dataset-relative path or a BIDS URI beginning bids::, not a
            subject-relative path.
        """,
        "Files were renamed after the sidecar was written.",
        """
            The empty-room recording is stored under a convention the dataset
            does not follow, for example as a separate subject whose label
            differs from the one referenced.
        """,
    ],
    resolution="""
        Confirm the empty-room recording is present in the dataset, then check the
        form of the path: it is relative to the dataset root, or a BIDS URI. If
        the recording was never included, add it. Empty-room recordings are
        commonly stored as their own subject or session, so the path usually
        begins with a different subject directory than the recording that
        references it.
    """,
)

add(
    "SOURCE_FILE_EXIST",
    description="""
        A Sources field names one or more files that do not exist in the dataset.
        Sources records the inputs a file was derived from.
    """,
    interpretation="""
        This is an error. The provenance chain the field records is broken at this
        link.
    """,
    why="""
        Sources is how a derived file states what it came from, which is the basis
        of reproducibility in derivative datasets. A reference that does not
        resolve means the chain cannot be followed back to the raw data.
    """,
    causes=[
        """
            The source file is in a different dataset, for example the raw dataset
            that a derivative was computed from, and is referenced without a URI
            that can reach it.
        """,
        "Files were renamed in the source dataset after the derivative was computed.",
        """
            The path is written at the wrong level. Sources takes dataset-relative
            paths or BIDS URIs.
        """,
        "The derivative was copied away from the dataset it was computed within.",
    ],
    resolution="""
        Check the path form first: dataset-relative, or a BIDS URI beginning
        bids::. When the source lives in another dataset, use a BIDS URI with the
        named dataset form and declare that dataset in the DatasetLinks field of
        dataset_description.json, which is what makes a cross-dataset reference
        resolvable.
    """,
)

add(
    "MISSING_RESOLUTION_DESCRIPTION",
    description="""
        A derivative file carries a res entity, but the Resolution metadata object
        in its sidecar has no entry describing that res label.
    """,
    interpretation="""
        This is an error. The entity is a short label, and the metadata is where
        the label's meaning is defined. Without the entry, the label names nothing.
    """,
    why="""
        Labels such as res-1 or res-hi are arbitrary strings chosen by whoever
        made the derivative. Their meaning exists only in the Resolution object,
        so an undefined label leaves the file's resolution undocumented.
    """,
    causes=[
        "The res entity was added to filenames without the corresponding metadata entry.",
        "The label in the filename does not exactly match the key in the Resolution object.",
        "The sidecar was copied from a derivative using different resolution labels.",
    ],
    resolution="""
        Add an entry to the Resolution object in the sidecar keyed by exactly the
        res label used in the filename, with a description of what that resolution
        is. Check the spelling and case, since the key must match the label
        exactly.
    """,
)

add(
    "MISSING_DENSITY_DESCRIPTION",
    description="""
        A derivative file carries a den entity, but the Density metadata object in
        its sidecar has no entry describing that label.
    """,
    interpretation="""
        This is an error, with the same structure as an undefined resolution
        label: the filename uses a label whose meaning is not defined anywhere.
    """,
    why="""
        Density labels describe the vertex density of surface data. The label is
        arbitrary, so without the metadata entry there is no way to know what
        density a surface file actually has.
    """,
    causes=[
        "The den entity was added without the corresponding metadata entry.",
        "The label in the filename does not match the key in the Density object.",
        "The sidecar was copied from a derivative using different density labels.",
    ],
    resolution="""
        Add an entry to the Density object keyed by exactly the den label used in
        the filename, describing the vertex density it denotes.
    """,
)

add(
    "ATLAS_DESCRIPTION_REQUIRED",
    description="""
        A file uses an atlas entity, but no corresponding atlas description JSON
        file was found.
    """,
    interpretation="""
        This is an error. An atlas label in a filename must be backed by a
        description of the atlas it names.
    """,
    why="""
        An atlas label alone does not say which parcellation was used, what its
        regions are, or where it came from. Without the description, results
        reported per region cannot be interpreted or compared with anything.
    """,
    causes=[
        "The description file was not created when the atlas was added.",
        "Its name does not match the atlas label used in the filenames.",
        "It sits at the wrong level in the dataset.",
    ],
    resolution="""
        Create the atlas description JSON named for the atlas label exactly as it
        appears in the filenames, and place it where the standard expects it.
        Describe the atlas, its provenance and its regions.
    """,
    confidence="medium",
)

add(
    "ATLAS_DESCRIPTION_RECOMMENDED",
    description="""
        A file uses an atlas entity and no corresponding atlas description JSON
        file was found. In this context the description is recommended rather than
        required.
    """,
    interpretation="""
        This is a warning. The dataset remains valid, but the atlas label is
        undocumented.
    """,
    why="""
        Without a description, the atlas label is a name with no definition
        attached, which makes regional results difficult to interpret or reuse
        even when everything else about the dataset is correct.
    """,
    causes=[
        "The description file was not created.",
        "Its name does not match the atlas label.",
    ],
    resolution="""
        Add an atlas description JSON named for the atlas label, describing the
        parcellation and where it came from. Although only recommended here, it is
        what allows anybody else to make sense of results reported per region.
    """,
    confidence="medium",
)


# ============================================================
# Privacy
#
# These warnings are about information that was not meant to be shared and that
# travels in places people do not look: header fields, file metadata, and the
# extremes of a distribution.
# ============================================================

add(
    "AGE_89",
    description="""
        A participant's age is 89 or above. Under the HIPAA de-identification
        standard, ages over 89 are identifying and should be handled specially.
    """,
    interpretation="""
        This is a warning about disclosure risk rather than about data
        correctness. The age may be perfectly accurate.
    """,
    why="""
        Very high ages are rare, so in combination with other attributes in a
        dataset they can single a person out. This is why age is commonly capped
        in shared datasets.
    """,
    causes=[
        "The participant genuinely is 89 or older.",
        "An age entered in the wrong unit, for example months recorded as years.",
        "A placeholder value such as 99 used to mean unknown.",
    ],
    resolution="""
        Confirm the value is a real age rather than a placeholder or a unit
        mistake. If it is real and the dataset will be shared, cap ages at 89 by
        recording 89 for every participant at or above that age, and say in the
        README or the participants.json that the column is capped. Note that the
        older convention of writing "89+" is deprecated, so the value should
        remain numeric.
    """,
)

add(
    "AGE_UNITS",
    description="""
        The Units value given for the age column in participants.json is not one
        of the ISO 8601 duration units the standard allows.
    """,
    interpretation="""
        This is a warning. The ages themselves may be right, but the unit is
        written in a form tools cannot interpret.
    """,
    why="""
        Age is a number whose meaning depends entirely on its unit. Datasets
        covering infants often record months or weeks, so the unit cannot be
        assumed, and an uninterpretable unit makes the column ambiguous.
    """,
    causes=[
        """
            The unit written as an English word, such as years or months, rather
            than in the form the standard expects.
        """,
        "An abbreviation such as yrs or mo.",
        "The Units field omitted, leaving the unit implicit.",
    ],
    resolution="""
        Set the Units value for the age column in participants.json to one of the
        ISO 8601-based duration units the standard allows. Check what unit the
        numbers are actually in before writing it, particularly in developmental
        datasets where mixing years and months across participants is a real risk.
    """,
)


def _gzip_header_family(code, field, what) -> None:
    add(
        code,
        description=f"""
            The gzip header of a compressed file contains a non-empty {field}.
            Gzip stores {what} alongside the compressed data.
        """,
        interpretation="""
            This is a warning with two distinct meanings, and it is worth working
            out which applies. It may be a disclosure risk, because the header can
            carry information from the machine where the file was compressed. It
            may also simply indicate that the file was not compressed
            reproducibly, so byte-identical inputs produce different files.
        """,
        why=f"""
            Gzip headers are not visible in any normal view of a dataset, so
            {what} can travel with a shared file without anyone noticing. The
            same fields also prevent checksums from matching between two
            compressions of identical data, which breaks verification and
            deduplication.
        """,
        causes=[
            """
                The file was compressed with a tool that records source metadata
                by default, which most command-line gzip implementations do.
            """,
            """
                The file was compressed as a separate step rather than written
                compressed by the conversion tool.
            """,
        ],
        resolution=f"""
            Recompress the file with the {field} suppressed. Most gzip
            implementations offer an option for this, and libraries that write
            compressed data directly usually leave these fields empty already.
            Before recompressing, check whether the existing {field} discloses
            anything sensitive, since that determines how urgent this is.
        """,
        notes="""
            This finding is about the compression container, not about the data
            inside it. Decompressing and recompressing changes nothing about the
            image or table the file holds.
        """,
    )


_gzip_header_family(
    "GZIP_HEADER_FILENAME", "filename field",
    "the original name of the file before compression",
)
_gzip_header_family(
    "GZIP_HEADER_COMMENT", "comment field",
    "a free-text comment",
)
_gzip_header_family(
    "GZIP_HEADER_MTIME", "timestamp", "the modification time of the original file",
)


# ============================================================
# Electrophysiology: electrodes, coordinates and channels
# ============================================================

add(
    "REQUIRED_COORDSYSTEM",
    description="""
        A dataset provides an electrodes.tsv or optodes.tsv file without the
        coordsystem.json that must accompany it.
    """,
    interpretation="""
        This is an error. The positions file gives coordinates as bare numbers;
        the coordinate system file says what those numbers mean.
    """,
    why="""
        Coordinates without a coordinate system cannot be placed. The same triple
        of numbers denotes completely different locations depending on the origin,
        the axis directions and the units, so without coordsystem.json the
        positions cannot be used for source localisation, plotting or
        registration.
    """,
    causes=[
        "The coordinate system file was not written when the positions were exported.",
        """
            Its entities do not match the positions file, so the two are not
            recognised as a pair.
        """,
        "It sits at a different level of the hierarchy than the positions file.",
    ],
    resolution="""
        Add a coordsystem.json beside the positions file with the same entities.
        It must state the coordinate system the positions are expressed in and
        their units, and where the system is defined by anatomical landmarks, it
        must describe those too.
    """,
)

add(
    "IEEG_ELECTRODES_REQUIRED",
    description="""
        An iEEG data file has no associated electrodes.tsv.
    """,
    interpretation="""
        This is an error. For intracranial recordings, electrode positions are not
        supplementary information, they are what identifies the recording sites.
    """,
    why="""
        Intracranial electrode placement is determined by clinical need and
        differs for every participant. Without positions, a channel name is an
        arbitrary label and there is no way to know what was recorded from where,
        which makes the recording uninterpretable and impossible to combine across
        participants.
    """,
    causes=[
        "Electrode positions were not localised, or were not exported into the dataset.",
        "The electrodes file exists but its entities do not match the recording.",
        """
            The file is placed where it cannot be inherited by the recording that
            needs it.
        """,
    ],
    resolution="""
        Add an electrodes.tsv giving each electrode's name and coordinates, along
        with the coordsystem.json that defines the coordinate space. A single
        electrodes file may serve several recordings from the same implantation:
        place it at the session level with the entities shared by those
        recordings, and inheritance will apply it to all of them.
    """,
)

add(
    "EXCESSIVE_ELECTRODE_SPECIFICITY",
    description="""
        An electrodes.tsv filename carries task, acquisition or run entities.
        Electrode positions rarely change between runs, so this level of
        specificity is usually unintended.
    """,
    interpretation="""
        This is a warning about file placement rather than content. The positions
        may be perfectly correct while being scoped more narrowly than they need
        to be.
    """,
    why="""
        A file named with a run entity applies only to that run. If the electrodes
        did not move, the same positions then have to be duplicated for every run,
        and duplicated files drift apart when one is corrected and the others are
        not.
    """,
    causes=[
        """
            The electrodes file was generated per recording by a conversion that
            copies the recording's entities.
        """,
        "The entities were copied from the data file without considering scope.",
        "The electrodes genuinely were re-placed between runs, which is unusual.",
    ],
    resolution="""
        Unless the electrodes genuinely moved, remove the task, acquisition and
        run entities from the electrodes filename so that one file covers every
        recording it applies to. Inheritance will then apply it to all of them. If
        positions really did change between runs, keeping the entities is correct
        and the warning can be left.
    """,
)

add(
    "EXCESSIVE_COORDSYSTEM_SPECIFICITY",
    description="""
        A coordsystem.json filename carries task, acquisition or run entities.
        Coordinate systems do not normally vary between runs.
    """,
    interpretation="""
        This is a warning about scope, with the same reasoning as an
        over-specified electrodes file.
    """,
    why="""
        A coordinate system is a property of how positions were measured, not of
        an individual recording. Scoping it to one run forces duplication across
        the others, and duplicates drift.
    """,
    causes=[
        "The file was generated per recording by a conversion that copies entities.",
        "Entities copied from the data file without considering scope.",
    ],
    resolution="""
        Remove the task, acquisition and run entities so the file applies to every
        recording that shares the coordinate system, and let inheritance do the
        rest.
    """,
)

add(
    "EMG_COORD_SYS_MISMATCH",
    description="""
        Values in the coordinate_system column of an electrodes.tsv do not appear
        as space entities in the corresponding coordsystem files.
    """,
    interpretation="""
        This is an error. Each coordinate system named in the table must have a
        coordinate system file declaring it.
    """,
    why="""
        The column names which system each electrode's coordinates are expressed
        in. If no file declares that system, the coordinates cannot be
        interpreted.
    """,
    causes=[
        "A coordinate system is named in the table but no matching coordsystem file exists.",
        "The space entity in the coordsystem filename is spelled differently from the column value.",
        "The electrodes table was written against a different set of coordinate system files.",
    ],
    resolution="""
        List the distinct values in the coordinate_system column and confirm each
        has a coordsystem file whose space entity matches exactly, including case.
        Add the missing files or correct the spelling so the two agree.
    """,
    confidence="medium",
)

add(
    "EMG_COORD_SYS_PARENTS",
    description="""
        A coordinate system refers to a parent coordinate system that was not
        found.
    """,
    interpretation="""
        This is an error. Coordinate systems can be defined relative to others,
        and the chain must resolve.
    """,
    why="""
        A coordinate system defined relative to a parent has no absolute meaning
        until the parent is found. A broken chain leaves the positions
        uninterpretable even though they are present.
    """,
    causes=[
        "The parent coordinate system file is missing from the dataset.",
        "The parent is named with a different spelling than the file declaring it.",
    ],
    resolution="""
        Identify the parent coordinate system named in the definition and confirm
        a file declares it. Add the missing definition or correct the name so the
        chain resolves.
    """,
    confidence="medium",
)

add(
    "COMPONENT_COLUMN_REQUIRED",
    description="""
        A channels.tsv contains ACCEL, GYRO or MAGN channels but has no component
        column.
    """,
    interpretation="""
        This is an error. Those channel types measure a vector quantity, so each
        channel records one axis and the component column says which.
    """,
    why="""
        Accelerometers, gyroscopes and magnetometers produce three channels
        measuring orthogonal axes. Without the component column there is no way to
        know which channel is which axis, so the vector cannot be reassembled and
        direction is lost.
    """,
    causes=[
        "The component column was not written when channels.tsv was generated.",
        """
            The axis is encoded in the channel name instead, for example as a
            suffix, which is not where the standard looks for it.
        """,
    ],
    resolution="""
        Add a component column to channels.tsv giving the axis each channel
        measures, using the values the standard defines. If the axis is currently
        encoded in the channel names, it can usually be extracted from them, but
        confirm the mapping rather than assuming an ordering.
    """,
)

add(
    "SHORT_CHANNEL_COUNT",
    description="""
        The ShortChannelCount metadata does not equal the number of channels
        marked true in the short_channel column of channels.tsv.
    """,
    interpretation="""
        This is an error. The sidecar summary and the channel table disagree about
        how many short-separation channels the recording has.
    """,
    why="""
        Short-separation channels sample superficial signal and are used to
        regress out systemic physiology. If the count is wrong, that regression
        either uses the wrong channels or is not applied to all of them, which
        changes every result derived from the recording.
    """,
    causes=[
        "Channels were added or removed and the count was not updated.",
        """
            The short_channel column uses values other than the booleans the
            standard defines, so the rows are not counted.
        """,
        "The sidecar was copied from a recording with a different montage.",
    ],
    resolution="""
        Count the rows in channels.tsv whose short_channel column is true, and set
        ShortChannelCount to that number. Check the column's values first, since a
        column written with 1 and 0 or with yes and no will not be counted as
        boolean.
    """,
)

add(
    "NIRS_SAMPLING_FREQUENCY",
    description="""
        The sampling frequency of a NIRS recording is not defined. It must be
        given either in the JSON sidecar or as a sampling_frequency column in
        channels.tsv.
    """,
    interpretation="""
        This is an error. NIRS allows the sampling frequency to be stated per
        channel, because channels can be sampled at different rates, but one of
        the two forms must be present.
    """,
    why="""
        Without a sampling frequency, the samples cannot be placed in time at all.
        Nothing about the recording's timing, including the alignment of events to
        data, can be determined.
    """,
    causes=[
        "The field was omitted from the sidecar and no per-channel column was provided.",
        """
            Channels are sampled at different rates so no single value was
            written, but the per-channel column was not added either.
        """,
        "The value was written under a different field name.",
    ],
    resolution="""
        Add SamplingFrequency to the JSON sidecar if every channel shares one
        rate. If rates differ between channels, add a sampling_frequency column to
        channels.tsv giving the rate for each. Take the values from the
        acquisition software rather than inferring them from the data length.
    """,
)

add(
    "NIRS_RECOMMENDED_CHANNELS",
    description="""
        A NIRS recording has no associated channels.tsv file.
    """,
    interpretation="""
        This is a warning: the standard recommends rather than requires the file
        here. In practice a NIRS recording is difficult to use without it.
    """,
    why="""
        NIRS channels are source-detector pairs at specific wavelengths. Which
        pair and which wavelength a channel corresponds to is not recoverable from
        the data file alone, so without channels.tsv the channels cannot be
        related to positions on the head.
    """,
    causes=[
        "The file was not exported alongside the recording.",
        "Its entities do not match the recording, so the two are not paired.",
    ],
    resolution="""
        Add a channels.tsv beside the recording listing every channel with its
        name, type, wavelength and the source and detector it connects, together
        with the sampling frequency if it varies per channel.
    """,
)


def _template_column_family(code, axis) -> None:
    add(
        code,
        description=f"""
            The template_{axis} column must be present when the {axis} column of an
            optodes or electrodes table contains n/a.
        """,
        interpretation=f"""
            This is an error. An n/a in the {axis} column means the actual position
            was not measured, and the template column is where the assumed
            position from a standard layout is recorded instead.
        """,
        why=f"""
            A missing coordinate with nothing in its place leaves the optode or
            electrode unlocalised. Recording the template position keeps the
            layout usable while making it explicit that the position is assumed
            rather than measured, which is a distinction that matters when
            interpreting results.
        """,
        causes=[
            """
                Positions were not digitised for this participant, so the measured
                columns were filled with n/a and no template positions were
                supplied.
            """,
            """
                Digitisation failed for some points and the gaps were marked n/a
                without a fallback.
            """,
        ],
        resolution=f"""
            Add a template_{axis} column giving the position from the standard
            layout used for the recording. Every row whose {axis} is n/a must have
            a template value. If positions were digitised after all, filling in the
            measured {axis} column is the better fix, since measured positions are
            preferable to template ones.
        """,
    )


_template_column_family("REQUIRED_TEMPLATE_X", "x")
_template_column_family("REQUIRED_TEMPLATE_Y", "y")
_template_column_family("REQUIRED_TEMPLATE_Z", "z")

add(
    "ELEKTA_NEUROMAG_DEPRECATED",
    description="""
        A coordinate system field uses the value "ElektaNeuromag", which is
        deprecated. The current value is "NeuromagElektaMEGIN".
    """,
    interpretation="""
        This is a warning about a renamed value rather than a wrong one. The
        coordinate system is the same; the name for it in the standard has
        changed to reflect the manufacturer's own change of name.
    """,
    why="""
        Coordinate system names are matched exactly by software deciding how to
        interpret positions. A deprecated name may not be recognised by tools
        written against the current standard, which then cannot place the sensors.
    """,
    causes=[
        "The dataset was converted with a tool written against an earlier version of the standard.",
        "The value was carried over from an older dataset or template.",
    ],
    resolution="""
        Replace "ElektaNeuromag" with "NeuromagElektaMEGIN" wherever it appears.
        Check every coordinate system field in the sidecar, not only the one
        reported, since the same value is often written into several of them:
        the fields for anatomical landmarks, digitised head points, head coils,
        fiducials, and the EEG, MEG and NIRS coordinate systems.
    """,
)


# ============================================================
# HED annotation
#
# HED is a controlled vocabulary for describing events in a way machines can
# reason about. It is optional in BIDS, but once a dataset uses it, its strings
# are validated against the HED schema, and these findings come from that
# separate validation rather than from BIDS rules.
# ============================================================

add(
    "HED_ERROR",
    description="""
        A HED annotation string failed HED validation. HED is a controlled
        vocabulary used to describe events; its strings are checked against the
        HED schema rather than against BIDS rules.
    """,
    interpretation="""
        This is an error, and it concerns the annotation rather than the BIDS
        structure around it. The events file may be perfectly well formed as a
        BIDS table while a HED string inside it is invalid.
    """,
    why="""
        HED exists so that events can be searched and compared across datasets
        without knowing each study's private labels. An invalid string is not
        machine-readable, so the annotation gives up the benefit it was added for.
    """,
    causes=[
        """
            A tag that does not exist in the HED schema version in use, often
            because of a spelling difference or a tag that was renamed between
            versions.
        """,
        "Unbalanced parentheses in a tag group.",
        "A value given for a tag that does not take one, or omitted where one is required.",
        "A tag used outside the part of the hierarchy where it is defined.",
        """
            The annotation was written against a different HED schema version than
            the one the dataset declares.
        """,
    ],
    resolution="""
        Read the accompanying message, which names the offending tag and the
        reason. Check the tag against the HED schema version your dataset
        declares in HEDVersion, since a tag valid in one version may not exist in
        another. The HED validator and the online HED tools give more detail than
        the BIDS validator relays.
    """,
    confidence="medium",
)

add(
    "HED_WARNING",
    description="""
        A HED annotation string produced a warning during HED validation.
    """,
    interpretation="""
        This is a warning. The annotation is usable, but HED validation has found
        something worth reviewing, typically a stylistic or completeness issue
        rather than an outright error.
    """,
    why="""
        HED warnings often point at annotations that are valid but less useful
        than intended, for example a tag that is deprecated or one that is more
        general than the situation allows. They affect how well the annotation
        supports later search and comparison.
    """,
    causes=[
        "A deprecated tag that still validates but has a preferred replacement.",
        "An annotation less specific than the schema allows.",
        "Style issues in the annotation string.",
    ],
    resolution="""
        Read the message for the specific concern and decide whether to act. These
        are worth addressing before sharing a dataset, since HED annotation is
        usually added precisely so that others can reuse the events.
    """,
    confidence="medium",
)

add(
    "HED_MISSING_VALUE_IN_SIDECAR",
    description="""
        A value appearing in a column of an events file has no corresponding key
        in the HED annotation of the JSON sidecar, so that value is not annotated.
    """,
    interpretation="""
        This is a warning. HED annotation of a categorical column works by mapping
        each possible value to a HED string in the sidecar, and one of the values
        present in the data has no entry.
    """,
    why="""
        Unannotated values are invisible to any HED-based search. If a condition
        appears in the data but not in the annotation, analyses that select trials
        by HED tag will silently omit it.
    """,
    causes=[
        """
            A new value appeared in the events data after the sidecar annotation
            was written.
        """,
        """
            The value in the data differs from the key in the sidecar by case,
            whitespace or spelling.
        """,
        """
            Values that were not expected, such as a response code for a missed
            trial, were never annotated.
        """,
    ],
    resolution="""
        List the distinct values actually present in the column and compare them
        against the keys in the sidecar's HED object for that column. Add an entry
        for each missing value. Check for differences in case and stray whitespace
        first, since those produce a mismatch that looks like an omission.
    """,
    confidence="medium",
)

add(
    "HED_VERSION_NOT_DEFINED",
    description="""
        The dataset uses HED annotations but does not declare HEDVersion in
        dataset_description.json.
    """,
    interpretation="""
        This is a warning. Validation proceeds using a default version, which may
        not be the one the annotations were written against.
    """,
    why="""
        HED tags change between schema versions: tags are added, renamed and
        deprecated. Without a declared version, an annotation is being interpreted
        against an assumed vocabulary, so the same string can validate today and
        fail later, or mean something slightly different.
    """,
    causes=[
        "HED annotations were added without updating dataset_description.json.",
        "The field was written under a different name or in the wrong file.",
    ],
    resolution="""
        Add HEDVersion to dataset_description.json naming the HED schema version
        the annotations were written against. If you do not know which was used,
        validate against a candidate version and check the annotations pass before
        declaring it.
    """,
    confidence="medium",
)

add(
    "HED_INTERNAL_ERROR",
    description="""
        The HED validator encountered an internal error while checking an
        annotation.
    """,
    interpretation="""
        This is an error in the HED validation tooling rather than a statement
        about your annotation. The consequence is that the annotation was not
        fully checked.
    """,
    why="""
        An annotation that was never checked may still be invalid. Treating the
        absence of a finding as a pass is unsafe here.
    """,
    causes=[
        "A version mismatch between the HED validator and the declared schema version.",
        "An annotation in a form the validator did not anticipate.",
        "The declared HED schema could not be retrieved.",
    ],
    resolution="""
        Update the validator and its HED components and run again. Confirm that
        the HEDVersion declared in dataset_description.json names a schema version
        that exists and can be fetched. If the error persists, validate the
        annotations with the standalone HED tools, which report more detail.
    """,
    confidence="medium",
)

add(
    "HED_INTERNAL_WARNING",
    description="""
        The HED validator produced an internal warning while checking an
        annotation.
    """,
    interpretation="""
        This is a warning from the tooling rather than about your data. Part of
        the HED validation may not have run as intended.
    """,
    why="""
        As with the internal error, the practical risk is a quiet gap in
        coverage: annotations that were not fully checked are reported the same
        way as annotations that passed.
    """,
    causes=[
        "A version mismatch between the validator and the declared HED schema.",
        "An annotation form the validator handled incompletely.",
    ],
    resolution="""
        Update the validator and confirm the declared HEDVersion resolves to a
        real schema. If you rely on the annotations, check them with the
        standalone HED tools as well.
    """,
    confidence="medium",
)


# ============================================================
# Arterial spin labelling: the M0 scan
#
# M0Type declares how the equilibrium magnetisation image was obtained. The
# declaration and the files present have to agree, because quantification reads
# the declaration to decide where to find the M0 image.
# ============================================================

add(
    "M0Type_SET_INCORRECTLY",
    description="""
        M0Type is set to "separate", which declares that the dataset contains a
        standalone m0scan, but no associated m0scan.nii[.gz] and m0scan.json were
        found.
    """,
    interpretation="""
        This is an error. The declaration and the files disagree: the sidecar
        promises a separate M0 image that is not there.
    """,
    why="""
        The M0 image provides the equilibrium magnetisation that ASL
        quantification divides by to convert the perfusion-weighted signal into
        cerebral blood flow. Without it, quantification cannot proceed or falls
        back to an assumed value, which changes every perfusion estimate.
    """,
    causes=[
        "The M0 series was not converted into the dataset.",
        """
            The m0scan file is present but its entities do not match the ASL
            recording, so the two are not paired.
        """,
        """
            The M0 volume is actually included within the ASL time series, in
            which case M0Type should be "included" rather than "separate".
        """,
        "The M0 image was placed in the wrong directory.",
    ],
    resolution="""
        Establish where the M0 information really is. If a separate M0 series was
        acquired, convert it and place it in the perf directory with entities
        matching the ASL recording, alongside its own JSON sidecar. If the M0
        volume is part of the ASL time series and listed in aslcontext.tsv, set
        M0Type to "included" instead. If no M0 was acquired at all, the correct
        value depends on how quantification is intended to proceed, and the other
        permitted values cover an estimated value or its absence.
    """,
)

add(
    "M0Type_SET_INCORRECTLY_TO_ABSENT",
    description="""
        M0Type is set to "absent", declaring that no M0 information exists, but a
        separate m0scan.nii[.gz] and m0scan.json are present in the dataset.
    """,
    interpretation="""
        This is an error, and it is the reverse of the previous one: the files are
        there and the sidecar denies them.
    """,
    why="""
        Quantification reads M0Type to decide where to obtain the equilibrium
        magnetisation. Declared absent, an M0 image that exists will not be used,
        so the perfusion estimates are computed from an assumption when real data
        was available.
    """,
    causes=[
        """
            The sidecar was written before the M0 series was added, and not
            updated afterwards.
        """,
        """
            M0Type was left at a template default of "absent" while the data was
            converted correctly.
        """,
        "The sidecar was copied from an acquisition that genuinely had no M0.",
    ],
    resolution="""
        Set M0Type to "separate", since a standalone m0scan is present, and
        confirm that the m0scan's entities match the ASL recording so the two are
        paired. Check the other ASL sidecars in the dataset for the same
        template default.
    """,
)

add(
    "M0Type_SET_INCORRECTLY_TO_ABSENT_IN_ASLCONTEXT",
    description="""
        M0Type is set to "absent", but the associated aslcontext.tsv lists an
        m0scan volume within the ASL time series.
    """,
    interpretation="""
        This is an error. The volume list says an M0 volume was acquired as part
        of the series, and the sidecar says none exists.
    """,
    why="""
        The M0 volume inside the time series is what quantification would use.
        Declaring it absent means it is skipped, and the perfusion calculation
        proceeds without the calibration image that is sitting in the data.
    """,
    causes=[
        """
            M0Type was left at a template default while aslcontext.tsv was written
            correctly from the acquisition.
        """,
        """
            The sidecar was copied from an acquisition whose M0 handling was
            different.
        """,
    ],
    resolution="""
        Set M0Type to "included", which is the value for an M0 volume contained
        within the ASL time series, and confirm that aslcontext.tsv marks the
        correct volume as m0scan. The two statements then agree and quantification
        can find the volume.
    """,
)


# ============================================================
# Remaining modality-specific checks
# ============================================================

add(
    "PHASE_UNITS",
    description="""
        A phase image, identified by the part-phase entity, does not declare its
        units as either "rad" or "arbitrary".
    """,
    interpretation="""
        This is an error. Phase data is only interpretable once its scale is
        known, and the standard permits exactly these two declarations.
    """,
    why="""
        Scanners write phase either in radians or in arbitrary integer units that
        span the vendor's own range. Software that unwraps phase or computes a
        field map has to know which, because the same numbers mean different
        angles under each convention. Getting it wrong scales every derived field
        value.
    """,
    causes=[
        "The Units field was omitted from the phase image's sidecar.",
        """
            The units are declared using another spelling, such as radians or a
            vendor-specific term, rather than the permitted values.
        """,
        """
            The conversion did not know the scaling, which is common when phase is
            written in the vendor's raw integer range.
        """,
    ],
    resolution="""
        Set Units in the phase image's sidecar to "rad" if the values are in
        radians, or "arbitrary" if they are in the scanner's own integer range.
        Inspect the value range to decide: data spanning roughly minus pi to pi is
        in radians, while a wide symmetric integer range is arbitrary units.
    """,
)

add(
    "MRS_MATRIX_SIZE",
    description="""
        The MatrixSize metadata of a magnetic resonance spectroscopy file does not
        match the first three dimensions of the NIfTI header.
    """,
    interpretation="""
        This is an error. The metadata and the image header state the same
        spatial extent and disagree.
    """,
    why="""
        For spectroscopy, the spatial dimensions determine how voxels are laid out
        relative to anatomy. A mismatch means the metadata describes a different
        acquisition geometry than the file contains, so spectra cannot be placed
        reliably.
    """,
    causes=[
        "The sidecar was copied from an acquisition with a different matrix.",
        """
            The image was reshaped or resampled after the sidecar was written.
        """,
        "The dimensions were written in a different order than the header uses.",
    ],
    resolution="""
        Read the first three dimensions from the NIfTI header and compare them
        against MatrixSize. Check the ordering before assuming the values are
        wrong, since a transposed but otherwise correct triple is a common cause.
        Correct whichever does not match the acquisition.
    """,
    confidence="medium",
)

add(
    "MRS_NIFTI_CONSISTENCY",
    description="""
        The ResonantNucleus or SpectrometerFrequency fields are inconsistent
        between the JSON sidecar and the NIfTI-MRS header extension.
    """,
    interpretation="""
        This is an error. Spectroscopy data carries these parameters in two
        places, and they disagree.
    """,
    why="""
        The resonant nucleus and the spectrometer frequency together set the
        frequency axis of every spectrum. If they are inconsistent, chemical
        shifts are computed against the wrong reference, so peaks are assigned to
        the wrong metabolites.
    """,
    causes=[
        "The sidecar was written by hand or from a template rather than from the data.",
        "The sidecar was copied from an acquisition on a different scanner or with a different nucleus.",
        "A conversion wrote the header extension and the sidecar from different sources.",
    ],
    resolution="""
        Take the values from the NIfTI-MRS header extension, which is written
        directly from the acquisition, and make the sidecar agree with it unless
        you have specific reason to believe the extension is wrong. Confirm the
        nucleus matches the acquisition, since a proton acquisition described as
        another nucleus shifts the whole frequency axis.
    """,
    confidence="medium",
)

add(
    "PIXEL_SIZE_INCONSISTENT",
    description="""
        The PixelSize metadata of a microscopy image is inconsistent with the
        PhysicalSizeX, PhysicalSizeY and PhysicalSizeZ values in the image's own
        OME metadata.
    """,
    interpretation="""
        This is an error. The physical size of a pixel is stated in two places and
        the two disagree.
    """,
    why="""
        Everything quantitative in microscopy depends on pixel size: cell counts
        per unit area, distances, areas, volumes. An inconsistency means any
        measurement in physical units could be wrong, and there is no way to tell
        which of the two sources was used.
    """,
    causes=[
        "PixelSize was entered by hand and does not match what the acquisition recorded.",
        """
            The two are expressed in different units, so the same size reads as two
            different numbers.
        """,
        "The image was resampled or downscaled without PixelSize being updated.",
        "The sidecar was copied from an image acquired at a different magnification.",
    ],
    resolution="""
        Read the physical sizes from the image's OME metadata and compare them
        against PixelSize, checking units before values since a unit difference
        accounts for many of these. Set PixelSize to match, along with
        PixelSizeUnits. If the image was downscaled, the pixel size has changed by
        the same factor and must be recomputed rather than copied.
    """,
)

add(
    "INCONSISTENT_TIFF_EXTENSION",
    description="""
        A microscopy file's TIFF variant does not match its extension. The
        extension declares a particular TIFF flavour and the file contents are a
        different one.
    """,
    interpretation="""
        This is an error. As with any extension mismatch, the name makes a promise
        about the format that the contents do not keep.
    """,
    why="""
        Readers select a decoder from the extension. Microscopy uses several TIFF
        variants with different capabilities, so a file opened with the wrong
        decoder either fails or is read incorrectly, and large pyramidal images
        are particularly affected.
    """,
    causes=[
        "The file was renamed to a different TIFF extension without being converted.",
        """
            A conversion wrote one variant while naming the output for another.
        """,
        "An ordinary TIFF was given an extension reserved for a specific variant.",
    ],
    resolution="""
        Determine which TIFF variant the file actually is, using a microscopy
        image tool that reports the format. Then either convert the file to the
        variant its extension claims, or rename it to the extension that matches
        its contents. Renaming is usually correct when no conversion was intended.
    """,
    confidence="medium",
)

add(
    "INCOMPLETE_STIMULUS_PRESENTATION",
    description="""
        The StimulusPresentation metadata of an events file associated with an
        eye-tracking recording is incomplete. It must describe the presentation
        setup fully.
    """,
    interpretation="""
        This is an error about missing metadata rather than a wrong value. Some
        required part of the stimulus presentation description is absent.
    """,
    why="""
        Eye-tracking coordinates are only meaningful relative to the display they
        were recorded against. The screen's size, resolution and distance from the
        participant are what convert raw gaze coordinates into visual angle, so
        without them gaze data cannot be compared across setups or interpreted in
        degrees.
    """,
    causes=[
        """
            The stimulus presentation details were not recorded at acquisition
            time, which is the common case since they live in the lab setup rather
            than in any data file.
        """,
        "Only part of the required description was entered.",
        "The metadata was written into the wrong sidecar.",
    ],
    resolution="""
        Complete the StimulusPresentation object in the events file's sidecar,
        describing the presentation software and the screen: its resolution, its
        physical size and the viewing distance. These come from the lab setup, so
        recover them from the experiment documentation rather than estimating,
        since gaze in degrees of visual angle depends on them directly.
    """,
    confidence="medium",
)

add(
    "MISSING_ONSET_COLUMN",
    description="""
        A physioevents.tsv.gz file declares an OnsetSource in its sidecar naming a
        column that does not exist in the file.
    """,
    interpretation="""
        This is an error. OnsetSource points at the column holding event onsets,
        and it points at nothing.
    """,
    why="""
        Without a resolvable onset column, the events in the file cannot be placed
        in time at all, so they cannot be related to the recording they accompany.
    """,
    causes=[
        "The column name in OnsetSource is spelled differently from the column header.",
        "The column was renamed in the file without the sidecar being updated.",
        "The sidecar was copied from a recording whose columns differ.",
        "Case differences between the declared name and the header.",
    ],
    resolution="""
        Compare the OnsetSource value against the column headers in the file
        exactly, including case and any surrounding whitespace. Correct whichever
        is wrong so the declared name matches a real column.
    """,
    confidence="medium",
)

add(
    "UNKNOWN_PUPIL_SIZE",
    description="""
        The description of the pupil_size column in the JSON sidecar does not make
        clear whether the values are pupil area or pupil diameter.
    """,
    interpretation="""
        This is a warning. The data is present and usable; what is missing is the
        statement of which quantity it represents.
    """,
    why="""
        Area and diameter are related by a square, so treating one as the other
        distorts every pupillometric measure. Eye trackers differ in which they
        report, and some can be configured either way, so the quantity cannot be
        inferred from the equipment alone.
    """,
    causes=[
        """
            The description was left generic, saying only that the column holds
            pupil size.
        """,
        """
            The tracker's own terminology was copied without resolving which
            quantity it means.
        """,
        "The recording mode was not documented at acquisition time.",
    ],
    resolution="""
        Write the description of the pupil_size column to say explicitly whether
        the values are area or diameter, and give the units. Determine which from
        the eye tracker's configuration for this recording rather than from its
        default, since the setting is often changed per experiment.
    """,
    confidence="medium",
)
