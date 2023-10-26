from typing import ClassVar

from pants.backend.adhoc.target_types import AdhocToolRunnableField, AdhocToolArgumentsField, \
    AdhocToolRunnableDependenciesField, AdhocToolExecutionDependenciesField
from pants.base.exiter import PANTS_SUCCEEDED_EXIT_CODE
from pants.core.goals.lint import LintFilesRequest, LintResult
from pants.core.goals.run import RunFieldSet, RunInSandboxRequest
from pants.core.util_rules.adhoc_process_support import ResolvedExecutionDependencies, \
    ResolveExecutionDependenciesRequest, rules as adhoc_process_support_rules, ExtraSandboxContents, \
    MergeExtraSandboxContents
from pants.core.util_rules.environments import EnvironmentNameRequest
from pants.core.util_rules.partitions import Partitions, PartitionMetadata
from pants.engine.addresses import Addresses
from pants.engine.environment import EnvironmentName
from pants.engine.fs import PathGlobs
from pants.engine.internals.native_engine import FilespecMatcher, Snapshot, Address, AddressInput, Digest, EMPTY_DIGEST, \
    MergeDigests
from pants.engine.internals.selectors import Get
from pants.engine.process import Process, FallibleProcessResult
from pants.engine.rules import rule, collect_rules
from pants.engine.target import Targets, FieldSetsPerTarget, FieldSetsPerTargetRequest, Target, COMMON_TARGET_FIELDS, \
    StringSequenceField
from pants.option.option_types import SkipOption
from pants.option.subsystem import Subsystem
from pants.util.frozendict import FrozenDict

"""
ByoTool(
    goal="lint",
    options_scope='byo_flake8',
    name="byo_Flake8",
    help="Flake8 linter using the flake8 you have specified in a resolve",
    executable=PythonToolExecutable(
        main=ConsoleScript("flake8"),
        requirements=["flake8>=5.0.4,<7"],
        resolve="byo_flake8",
    ),
    file_glob_include=["**/*.py"],
    file_glob_exclude=["pants-plugins/**"],
),
"""


class ByoFileGlobIncludeField(StringSequenceField):
    alias: ClassVar[str] = "file_glob_include"
    required = True


class ByoFileGlobExcludeField(StringSequenceField):
    alias: ClassVar[str] = "file_glob_exclude"
    required = False
    default = ()


class ByoLinterTarget(Target):
    alias: ClassVar[str] = "byolinter"
    core_fields = (
        *COMMON_TARGET_FIELDS,
        AdhocToolRunnableField,
        AdhocToolArgumentsField,
        AdhocToolExecutionDependenciesField,
        AdhocToolRunnableDependenciesField,
        ByoFileGlobIncludeField,
        ByoFileGlobExcludeField,
    )



class ByoTool(Subsystem):
    options_scope = 'byotool'
    name = 'ByoTool'
    help = 'Bring your own Tool'

    skip = SkipOption('lint')
    # linter = '//:flake8_linter'
    linter = '//:markdownlint_linter'


class ByoToolRequest(LintFilesRequest):
    tool_subsystem = ByoTool


@rule
async def partition_inputs(
        request: ByoToolRequest.PartitionRequest,
        subsystem: ByoTool
) -> Partitions[str, PartitionMetadata]:
    if subsystem.skip:
        return Partitions()

    linter_address_str = subsystem.linter
    linter_address = await Get(Address, AddressInput,
                                 AddressInput.parse(
                                    linter_address_str,
                                     description_of_origin=f"ByoTool linter target"
                                 ))

    addresses = Addresses((linter_address,))
    addresses.expect_single()

    linter_targets = await Get(Targets, Addresses, addresses)
    linter = linter_targets[0]

    matching_filepaths = FilespecMatcher(
        includes=linter[ByoFileGlobIncludeField].value,
        excludes=linter[ByoFileGlobExcludeField].value
    ).matches(request.files)

    return Partitions.single_partition(sorted(matching_filepaths))


@rule
async def run_byotool(request: ByoToolRequest.Batch,
                      subsystem: ByoTool) -> LintResult:
    sources_snapshot = await Get(Snapshot, PathGlobs(request.elements))

    linter_address_str = subsystem.linter
    linter_address = await Get(Address, AddressInput,
                                 AddressInput.parse(
                                    linter_address_str,
                                     description_of_origin=f"ByoTool linter target"
                                 ))

    addresses = Addresses((linter_address,))
    addresses.expect_single()

    linter_targets = await Get(Targets, Addresses, addresses)
    linter = linter_targets[0]


    runnable_address_str = linter[AdhocToolRunnableField].value
    runnable_address = await Get(Address, AddressInput,
                                 AddressInput.parse(
                                    runnable_address_str,
                                     # Need to add a relative_to=addresses
                                     description_of_origin=f"ByoTool runnable target"
                                 ))

    addresses = Addresses((runnable_address,))
    addresses.expect_single()

    runnable_targets = await Get(Targets, Addresses, addresses)

    target = runnable_targets[0]

    environment_name = await Get(
        EnvironmentName, EnvironmentNameRequest, EnvironmentNameRequest.from_target(target)
    )

    field_sets = await Get(
        FieldSetsPerTarget, FieldSetsPerTargetRequest(RunFieldSet, runnable_targets)
    )

    run_field_set: RunFieldSet = field_sets.field_sets[0]

    run_request = await Get(
        RunInSandboxRequest, {environment_name: EnvironmentName, run_field_set: RunFieldSet}
    )

    execution_environment = await Get(
        ResolvedExecutionDependencies,
        ResolveExecutionDependenciesRequest(
            target.address,
            execution_dependencies=linter[AdhocToolExecutionDependenciesField].value,
            runnable_dependencies=linter[AdhocToolRunnableDependenciesField].value
        ),
    )
    dependencies_digest = execution_environment.digest
    runnable_dependencies = execution_environment.runnable_dependencies


    extra_env: dict[str, str] = dict(run_request.extra_env or {})
    extra_path = extra_env.pop("PATH", None)

    extra_sandbox_contents = []

    extra_sandbox_contents.append(
        ExtraSandboxContents(
            EMPTY_DIGEST,
            extra_path,
            run_request.immutable_input_digests or FrozenDict(),
            run_request.append_only_caches or FrozenDict(),
            run_request.extra_env or FrozenDict(),
        )
    )

    if runnable_dependencies:
        extra_sandbox_contents.append(
            ExtraSandboxContents(
                EMPTY_DIGEST,
                f"{{chroot}}/{runnable_dependencies.path_component}",
                runnable_dependencies.immutable_input_digests,
                runnable_dependencies.append_only_caches,
                runnable_dependencies.extra_env,
            )
        )

    merged_extras = await Get(
        ExtraSandboxContents, MergeExtraSandboxContents(tuple(extra_sandbox_contents))
    )
    extra_env = dict(merged_extras.extra_env)
    if merged_extras.path:
        extra_env["PATH"] = merged_extras.path

    input_digest = await Get(Digest, MergeDigests((dependencies_digest, run_request.digest, sources_snapshot.digest)))

    extra_args = linter[AdhocToolArgumentsField].value or ()

    append_only_caches = {
        **merged_extras.append_only_caches,
    }

    proc = Process(
        argv=tuple(run_request.args + extra_args + sources_snapshot.files),
        description='Running byotool',
        input_digest=input_digest,
        append_only_caches=append_only_caches,
        immutable_input_digests=FrozenDict.frozen(merged_extras.immutable_input_digests),
        env=FrozenDict(extra_env)
    )

    proc_result = await Get(FallibleProcessResult, Process, proc)

    return LintResult.create(request, proc_result)
    # return LintResult(PANTS_SUCCEEDED_EXIT_CODE, "success", "", "byotool")

# A requirement is runnable: //:flake8 is the name of the target. Could use it.

def rules():
    return [
        *collect_rules(),
        *ByoToolRequest.rules(),
        *adhoc_process_support_rules(),
    ]