from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from repo_maintainer.retrospective_connectors import OpenAIEmbedder


class EmbeddingBatchTests(TestCase):
    def embedder(self, create):
        embedder = object.__new__(OpenAIEmbedder)
        embedder.model = 'test-model'
        embedder.client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
        return embedder

    def test_large_request_keeps_text_vector_alignment_across_reordered_batches(self):
        def provider(*, model, input):
            self.assertEqual(model, 'test-model')
            self.assertLessEqual(len(input), 20)
            return SimpleNamespace(data=[
                SimpleNamespace(index=i, embedding=[float(value)])
                for i, value in reversed(list(enumerate(input)))
            ])

        create = Mock(side_effect=provider)
        result = self.embedder(create).embed_texts([str(i) for i in range(45)])
        self.assertEqual(result, [[float(i)] for i in range(45)])
        self.assertEqual([len(call.kwargs['input']) for call in create.call_args_list], [20, 20, 5])

    def test_empty_input_does_not_call_provider(self):
        create = Mock()
        self.assertEqual(self.embedder(create).embed_texts([]), [])
        create.assert_not_called()

    def test_missing_or_duplicate_response_indices_are_rejected(self):
        for indices in ([0], [0, 0], [0, 2]):
            with self.subTest(indices=indices):
                create = Mock(return_value=SimpleNamespace(data=[
                    SimpleNamespace(index=i, embedding=[1.0]) for i in indices
                ]))
                with self.assertRaisesRegex(ValueError, 'indices'):
                    self.embedder(create).embed_texts(['one', 'two'])
