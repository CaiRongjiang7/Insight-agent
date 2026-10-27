import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from repo_maintainer.retrospective_connectors import OpenAIReviewGenerator
from repo_maintainer.retrospective_models import IncidentRecord
from test_retrospective_pipeline import FakeCollaboration, FakeGenerator, FakeGit


class ReviewModelOptionsTests(TestCase):
    def generate(self, enable_thinking=None, errors=None):
        incident = IncidentRecord(title='upload failure', metadata={
            'generation_validation_errors': errors or [],
        })
        evidence = FakeGit().collect(incident) + FakeCollaboration().collect(incident)
        document, _ = FakeGenerator().generate(incident, evidence, [])
        response = SimpleNamespace(
            output=[SimpleNamespace(type='function_call', name='submit_retrospective',
                                    arguments=document.model_dump_json())],
            model='test-model', usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )
        client = Mock()
        client.responses.create.return_value = response
        with patch('openai.OpenAI', return_value=client):
            generator = OpenAIReviewGenerator(api_key='test-only', model='test-model',
                                              enable_thinking=enable_thinking)
        actual, _ = generator.generate(incident, evidence, [])
        self.assertEqual(actual, document)
        return client.responses.create.call_args.kwargs

    def test_provider_extension_is_omitted_by_default_and_preserves_false(self):
        for setting in (None, False, True):
            with self.subTest(setting=setting):
                kwargs = self.generate(setting)
                if setting is None:
                    self.assertNotIn('extra_body', kwargs)
                else:
                    self.assertEqual(kwargs['extra_body'], {'enable_thinking': setting})
                self.assertEqual(kwargs['tool_choice'], 'required')
                self.assertFalse(kwargs['store'])

    def test_validation_feedback_reaches_model_as_feedback_not_evidence(self):
        errors = ['Quote not found in evidence ev_example']
        kwargs = self.generate(False, errors)
        payload = json.loads(kwargs['input'])
        self.assertEqual(payload['incident']['previous_validation_errors'], errors)
        self.assertTrue(all('generation_validation_errors' not in item for item in payload['evidence']))
