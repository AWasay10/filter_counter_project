import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:image_picker/image_picker.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() => runApp(const FilterCounterApp());

class FilterCounterApp extends StatelessWidget {
  const FilterCounterApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Filter Counter',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
      home: const HomePage(),
    );
  }
}

// ---------------------------------------------------------------- settings

class AppSettings {
  String serverUrl;
  double? boxW;
  double? boxH;
  double diameter;

  AppSettings({
    this.serverUrl = 'http://192.168.1.10:5000',
    this.boxW,
    this.boxH,
    this.diameter = 7.8,
  });

  /// server address without a trailing slash, always with http://
  String get baseUrl {
    var u = serverUrl.trim().replaceAll(RegExp(r'/+$'), '');
    if (!u.startsWith('http://') && !u.startsWith('https://')) {
      u = 'http://$u';
    }
    return u;
  }

  static Future<AppSettings> load() async {
    final p = await SharedPreferences.getInstance();
    return AppSettings(
      serverUrl: p.getString('serverUrl') ?? 'http://192.168.1.10:5000',
      boxW: p.getDouble('boxW'),
      boxH: p.getDouble('boxH'),
      diameter: p.getDouble('diameter') ?? 7.8,
    );
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('serverUrl', serverUrl.trim());
    if (boxW != null) {
      await p.setDouble('boxW', boxW!);
    } else {
      await p.remove('boxW');
    }
    if (boxH != null) {
      await p.setDouble('boxH', boxH!);
    } else {
      await p.remove('boxH');
    }
    await p.setDouble('diameter', diameter);
  }
}

// ---------------------------------------------------------------- result model

class AppException implements Exception {
  final String message;
  AppException(this.message);
  @override
  String toString() => message;
}

class CountResult {
  final int count;
  final int rows;
  final int perRowMin;
  final int perRowMax;
  final int boxFilled;
  final double pitch;
  final double seconds;
  final double? capacity;
  final List<String> warnings;
  final Uint8List image;

  CountResult({
    required this.count,
    required this.rows,
    required this.perRowMin,
    required this.perRowMax,
    required this.boxFilled,
    required this.pitch,
    required this.seconds,
    required this.capacity,
    required this.warnings,
    required this.image,
  });

  factory CountResult.fromJson(Map<String, dynamic> j) {
    final rawWarnings = (j['warnings'] as List?) ?? const [];
    return CountResult(
      count: (j['count'] as num).toInt(),
      rows: (j['rows'] as num).toInt(),
      perRowMin: (j['per_row_min'] as num).toInt(),
      perRowMax: (j['per_row_max'] as num).toInt(),
      boxFilled: (j['boxfilled'] as num?)?.toInt() ?? 0,
      pitch: (j['pitch'] as num).toDouble(),
      seconds: (j['seconds'] as num).toDouble(),
      capacity: (j['capacity'] as num?)?.toDouble(),
      warnings: rawWarnings.map((e) => e.toString()).toList(),
      image: base64Decode(j['image'] as String),
    );
  }
}

// ---------------------------------------------------------------- home page

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final ImagePicker _picker = ImagePicker();
  AppSettings _settings = AppSettings();
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _loadSettings();
  }

  Future<void> _loadSettings() async {
    final s = await AppSettings.load();
    if (!mounted) return;
    setState(() => _settings = s);
  }

  Future<void> _pickAndCount(ImageSource source) async {
    XFile? file;
    try {
      file = await _picker.pickImage(
        source: source,
        maxWidth: 4096,
        maxHeight: 4096,
        imageQuality: 92,
      );
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = 'Could not open the photo picker: $e');
      return;
    }
    if (file == null) return; // cancelled
    await _count(file.path);
  }

  Future<void> _count(String path) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final uri = Uri.parse('${_settings.baseUrl}/count');
      final req = http.MultipartRequest('POST', uri);
      req.files.add(await http.MultipartFile.fromPath('image', path));
      req.fields['diameter'] = _settings.diameter.toString();
      if (_settings.boxW != null && _settings.boxH != null) {
        req.fields['box_w'] = _settings.boxW.toString();
        req.fields['box_h'] = _settings.boxH.toString();
      }

      final streamed = await req.send().timeout(const Duration(minutes: 3));
      final body = await streamed.stream.bytesToString();

      Map<String, dynamic> json;
      try {
        json = jsonDecode(body) as Map<String, dynamic>;
      } catch (_) {
        throw AppException(
            'Unexpected reply from the server (HTTP ${streamed.statusCode}). '
            'Check the server address in Settings.');
      }
      if (streamed.statusCode != 200) {
        throw AppException(
            (json['error'] ?? 'Server error ${streamed.statusCode}').toString());
      }

      final result = CountResult.fromJson(json);
      if (!mounted) return;
      setState(() => _busy = false);
      await Navigator.of(context).push(
        MaterialPageRoute(builder: (_) => ResultPage(result: result)),
      );
    } on AppException catch (e) {
      _fail(e.message);
    } on TimeoutException {
      _fail('The server took too long to answer. Is it still running?');
    } on FormatException {
      _fail('The server address is not valid. Check it in Settings.');
    } catch (e) {
      _fail('Could not reach the server at ${_settings.baseUrl}.\n'
          'Make sure the PC server is running, the phone is on the same Wi-Fi, '
          'and the address in Settings is correct.\n\n($e)');
    }
  }

  void _fail(String message) {
    if (!mounted) return;
    setState(() {
      _busy = false;
      _error = message;
    });
  }

  Future<String> _testConnection(String raw) async {
    try {
      final base = AppSettings(serverUrl: raw).baseUrl;
      final r = await http
          .get(Uri.parse('$base/health'))
          .timeout(const Duration(seconds: 6));
      if (r.statusCode == 200) return 'Connected - the server answered.';
      return 'The server answered with HTTP ${r.statusCode}.';
    } catch (e) {
      return 'Could not connect: $e';
    }
  }

  Future<void> _openSettings() async {
    final urlC = TextEditingController(text: _settings.serverUrl);
    final wC = TextEditingController(text: _settings.boxW?.toString() ?? '');
    final hC = TextEditingController(text: _settings.boxH?.toString() ?? '');
    final dC = TextEditingController(text: _settings.diameter.toString());
    String testResult = '';

    final saved = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setLocal) => AlertDialog(
          title: const Text('Settings'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  controller: urlC,
                  keyboardType: TextInputType.url,
                  decoration: const InputDecoration(
                    labelText: 'Server address',
                    hintText: 'http://192.168.1.10:5000',
                  ),
                ),
                const SizedBox(height: 10),
                OutlinedButton.icon(
                  onPressed: () async {
                    setLocal(() => testResult = 'Testing...');
                    final r = await _testConnection(urlC.text);
                    setLocal(() => testResult = r);
                  },
                  icon: const Icon(Icons.wifi_find),
                  label: const Text('Test connection'),
                ),
                if (testResult.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(testResult),
                  ),
                const Divider(height: 30),
                const Text('Optional: check against the box size'),
                const SizedBox(height: 4),
                Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: wC,
                        keyboardType: const TextInputType.numberWithOptions(decimal: true),
                        decoration: const InputDecoration(labelText: 'Box width (mm)'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: TextField(
                        controller: hC,
                        keyboardType: const TextInputType.numberWithOptions(decimal: true),
                        decoration: const InputDecoration(labelText: 'Box height (mm)'),
                      ),
                    ),
                  ],
                ),
                TextField(
                  controller: dC,
                  keyboardType: const TextInputType.numberWithOptions(decimal: true),
                  decoration: const InputDecoration(labelText: 'Filter diameter (mm)'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Save'),
            ),
          ],
        ),
      ),
    );

    if (saved == true) {
      double? parse(String t) => double.tryParse(t.trim().replaceAll(',', '.'));
      final s = AppSettings(
        serverUrl: urlC.text.trim().isEmpty ? _settings.serverUrl : urlC.text.trim(),
        boxW: parse(wC.text),
        boxH: parse(hC.text),
        diameter: parse(dC.text) ?? 7.8,
      );
      await s.save();
      if (mounted) setState(() => _settings = s);
    }
    urlC.dispose();
    wC.dispose();
    hC.dispose();
    dC.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Filter Counter'),
        actions: [
          IconButton(
            tooltip: 'Settings',
            icon: const Icon(Icons.settings),
            onPressed: _busy ? null : _openSettings,
          ),
        ],
      ),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 480),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.grid_view_rounded, size: 72, color: scheme.primary),
                  const SizedBox(height: 12),
                  Text(
                    'Count the filters in a box',
                    style: Theme.of(context).textTheme.headlineSmall,
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 8),
                  const Text(
                    'Take the photo from straight above, with the whole tube area in '
                    'the frame and no glare. Tap the tubes on the screen first so the '
                    'exposure is set on them.',
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 28),
                  if (_busy) ...[
                    const CircularProgress(),
                    const SizedBox(height: 16),
                    const Text(
                      'Sending the photo and counting...\nThis can take up to a minute.',
                      textAlign: TextAlign.center,
                    ),
                  ] else ...[
                    SizedBox(
                      width: double.infinity,
                      child: FilledButton.icon(
                        onPressed: () => _pickAndCount(ImageSource.camera),
                        icon: const Icon(Icons.photo_camera),
                        label: const Padding(
                          padding: EdgeInsets.symmetric(vertical: 14),
                          child: Text('Take a photo'),
                        ),
                      ),
                    ),
                    const SizedBox(height: 12),
                    SizedBox(
                      width: double.infinity,
                      child: FilledButton.tonalIcon(
                        onPressed: () => _pickAndCount(ImageSource.gallery),
                        icon: const Icon(Icons.photo_library),
                        label: const Padding(
                          padding: EdgeInsets.symmetric(vertical: 14),
                          child: Text('Choose from gallery'),
                        ),
                      ),
                    ),
                  ],
                  if (_error != null) ...[
                    const SizedBox(height: 20),
                    Card(
                      color: scheme.errorContainer,
                      child: Padding(
                        padding: const EdgeInsets.all(14),
                        child: Text(
                          _error!,
                          style: TextStyle(color: scheme.onErrorContainer),
                        ),
                      ),
                    ),
                  ],
                  const SizedBox(height: 24),
                  Text(
                    'Server: ${_settings.baseUrl}',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// small wrapper so the spinner has a fixed size
class CircularProgress extends StatelessWidget {
  const CircularProgress({super.key});

  @override
  Widget build(BuildContext context) {
    return const SizedBox(
      width: 48,
      height: 48,
      child: CircularProgressIndicator(strokeWidth: 5),
    );
  }
}

// ---------------------------------------------------------------- result page

class ResultPage extends StatelessWidget {
  final CountResult result;
  const ResultPage({super.key, required this.result});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('Result: ${result.count} filters')),
      body: Column(
        children: [
          Expanded(
            child: Container(
              color: Colors.black,
              width: double.infinity,
              child: InteractiveViewer(
                minScale: 0.5,
                maxScale: 16,
                child: Center(
                  child: Image.memory(
                    result.image,
                    fit: BoxFit.contain,
                    filterQuality: FilterQuality.medium,
                  ),
                ),
              ),
            ),
          ),
          InfoPanel(result: result),
        ],
      ),
    );
  }
}

class InfoPanel extends StatelessWidget {
  final CountResult result;
  const InfoPanel({super.key, required this.result});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final r = result;
    return SafeArea(
      top: false,
      child: Container(
        width: double.infinity,
        color: theme.colorScheme.surfaceContainerHighest,
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.baseline,
              textBaseline: TextBaseline.alphabetic,
              children: [
                Text('${r.count}', style: theme.textTheme.displaySmall),
                const SizedBox(width: 8),
                const Text('filters counted'),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              '${r.rows} rows, ${r.perRowMin}-${r.perRowMax} per row   |   '
              'spacing ${r.pitch.toStringAsFixed(1)} px   |   ${r.seconds.toStringAsFixed(1)} s',
              style: theme.textTheme.bodySmall,
            ),
            if (r.capacity != null)
              Text(
                'Theoretical maximum for this box: ${r.capacity!.toStringAsFixed(0)}',
                style: theme.textTheme.bodySmall,
              ),
            for (final w in r.warnings)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Icon(Icons.warning_amber_rounded, size: 18, color: Colors.orange),
                    const SizedBox(width: 6),
                    Expanded(child: Text(w, style: theme.textTheme.bodySmall)),
                  ],
                ),
              ),
            const SizedBox(height: 6),
            Text(
              'Pinch to zoom, drag to move.',
              style: theme.textTheme.bodySmall,
            ),
          ],
        ),
      ),
    );
  }
}
