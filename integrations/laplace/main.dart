import 'dart:convert';
import 'dart:io';
import 'dart:ui';

import 'package:flutter/widgets.dart';
import 'package:chaldea/generated/l10n.dart';
import 'package:chaldea/app/api/atlas.dart';
import 'package:chaldea/models/models.dart';

import 'simulator.dart';
import 'manual_simulator.dart';

final missingData = <String>{};

class MissingDataClient implements HttpClient {
  @override
  Future<HttpClientRequest> openUrl(String method, Uri url) async {
    missingData.add(url.toString());
    throw StateError('Missing public game data: $url');
  }
  @override
  dynamic noSuchMethod(Invocation invocation) => null;
}

class LocalDataOnly extends HttpOverrides {
  @override
  HttpClient createHttpClient(SecurityContext? context) => MissingDataClient();
}

Future<void> main() async {
  final jobPath = Platform.environment['FGOMAA_LAPLACE_JOB'];
  if (jobPath == null) exit(2);
  final jobFile = File(jobPath);
  final output = File('${jobFile.parent.path}/result.json');
  Map<String, dynamic> job = {};
  try {
    job = jsonDecode(jobFile.readAsStringSync()) as Map<String, dynamic>;
    WidgetsFlutterBinding.ensureInitialized();
    await S.load(const Locale('en'));
    await db.initiateForTest(testAppPath: '${jobFile.parent.path}/runtime');
    HttpOverrides.global = LocalDataOnly();
    inputData = Map<String, dynamic>.from(job['data'] as Map);
    AtlasApi.frozenCommonReleases = Map<String, dynamic>.from(inputData['common_releases'] as Map? ?? {});
    AtlasApi.frozenReleaseQuestRecords = Map<String, dynamic>.from(inputData['release_quest_records'] as Map? ?? {});
    db.gameData = GameData(
      servants: [for (final s in inputData['servants'] as List) Servant.fromJson(Map<String, dynamic>.from(s))],
      craftEssences: [for (final ce in inputData['craft_essences'] as List) CraftEssence.fromJson(Map<String, dynamic>.from(ce))],
      mysticCodes: {for (final mc in inputData['mystic_codes'] as List) mc['id'] as int: MysticCode.fromJson(Map<String, dynamic>.from(mc))},
      baseSkills: {for (final s in inputData['base_skills'] as List) s['id'] as int: BaseSkill.fromJson(Map<String, dynamic>.from(s))},
      constData: ConstGameData.fromJson(Map<String, dynamic>.from(inputData['const_data'] as Map)),
    );
    final request = Map<String, dynamic>.from(job['request'] as Map);
    if (request['protocol'] != 4) throw StateError('Account simulation protocol mismatch');
    if (request['mode'] == 'manual') {
      final result = await simulateManual(request);
      output.writeAsStringSync(jsonEncode({
        'job_id':job['job_id'], 'input_digest':job['input_digest'], 'mode':'manual',
        'success':missingData.isEmpty, 'missing_data':missingData.toList(), 'manual':result,
      }));
      exit(0);
    }
    var proposal = await simulate(request, {'name': 'proposal', 'random': 900, 'dynamic': true, 'guards': true});
    final normalProbe = proposal;
    if (proposal['success'] != true && request['allow_facecards'] == true) {
      final npOnly = Map<String, dynamic>.from(request)..['allow_facecards'] = false;
      proposal = await simulate(npOnly, {'name': 'np_fallback', 'random': 900, 'dynamic': false, 'guards': true});
    }
    final fixedRequest = Map<String, dynamic>.from(request);
    fixedRequest['strategy'] = proposal['compiled_strategy'];
    final validation = proposal['success'] == true
        ? await simulate(fixedRequest, {'name': 'validation', 'random': 900, 'dynamic': false, 'guards': true, 'strict': true})
        : proposal;
    final unsupported = (validation['debug_messages'] as List).where((message) =>
        RegExp(r'not implemented|unhandled|unsupported|unknown function', caseSensitive: false).hasMatch(message as String)).toList();
    if (unsupported.isNotEmpty) {
      validation['success'] = false;
      validation['stop_reason'] = '当前战斗包含尚未实现的机制，无法确认';
      (validation['battle_errors'] as List).addAll(unsupported);
    }
    output.writeAsStringSync(jsonEncode({
      'job_id': job['job_id'], 'input_digest': job['input_digest'],
      'engine': 'Chaldea Laplace', 'engine_revision': '9970a57a7d150d5d551f583f6b2c4862a9f7e798',
      'success': missingData.isEmpty, 'missing_data': missingData.toList(),
      'proposal': proposal, 'validation': validation, 'compiled_strategy': proposal['compiled_strategy'],
      if (!identical(normalProbe, proposal)) 'facecard_probe': normalProbe,
    }));
    exit(0);
  } catch (error, stack) {
    output.writeAsStringSync(jsonEncode({
      'job_id': job['job_id'], 'input_digest': job['input_digest'],
      'success': false, 'message': error.toString(), 'detail': stack.toString(),
      'missing_data': missingData.toList(),
    }));
    exit(1);
  }
}
