import { afterEach, describe, expect, it, vi } from 'vitest'
import { harnessApi } from '../services/api'
import { fact, hint, intent, objectPage, summary } from '../test/blackboard'
import { loadGraphObjects, objectRef, projectKnowledgeGraph, refKey, relations } from './knowledgeGraph'

afterEach(() => vi.restoreAllMocks())

describe('knowledge relation projection', () => {
  it('keeps production ancestry and proof dependencies distinct, including reciprocal semantic links', () => {
    const observation = { ...fact, derived_from: [objectRef(hint)],
      verification: { ...fact.verification, premise_refs: [objectRef(intent)] },
      links: [{ relation: 'supports' as const, target: objectRef(hint) }, { relation: 'contributes_to' as const, target: objectRef(intent) }] }
    const clue = { ...hint, links: [{ relation: 'contradicts' as const, target: objectRef(fact) }] }
    const goal = { ...intent, satisfied_by: [objectRef(fact)] }
    const graph = projectKnowledgeGraph([observation, clue, goal], objectRef(goal))
    expect(graph.causalCycle).toBe(false)
    expect(new Set(graph.edges.map(edge => edge.relation))).toEqual(new Set([
      'derived_from', 'verification_premise', 'supports', 'contradicts', 'contributes_to', 'satisfied_by',
    ]))
    const origin = graph.nodes.find(node => node.ref.object_id === hint.id)!
    const result = graph.nodes.find(node => node.ref.object_id === fact.id)!
    const root = graph.nodes.find(node => node.root)!
    expect(origin.x).toBeLessThan(result.x)
    expect(result.x).toBeLessThan(root.x)
    expect(graph.edges.find(edge => edge.relation === 'verification_premise')?.owner).toEqual(objectRef(fact))
  })

  it('never silently redirects an old reference to the current revision', () => {
    const latest = { ...hint, revision: 2, suggestion: 'New text' }
    const result = { ...fact, derived_from: [objectRef(hint)] }
    const graph = projectKnowledgeGraph([latest, result, intent], objectRef(intent))
    const old = graph.nodes.find(node => node.key === refKey(objectRef(hint)))!
    expect(old.object).toBeNull()
    expect(old.current).toBe(false)
    expect(graph.nodes.find(node => node.key === refKey(objectRef(latest)))?.object).toBe(latest)
    const expanded = projectKnowledgeGraph([latest, result, intent], objectRef(intent), [hint])
    expect(expanded.nodes.find(node => node.key === old.key)?.object?.revision).toBe(1)
    expect(expanded.nodes.filter(node => node.ref.object_id === hint.id)).toHaveLength(2)
  })

  it('includes explicit branch, source, replacement and blocker references without inventing edges', () => {
    const branch = { ...intent, id: 'child', parent_intent_ref: objectRef(intent), root_intent_ref: objectRef(intent), blockers: [objectRef(fact)] }
    expect(relations(branch).map(edge => edge.relation)).toEqual(['parent_intent_ref', 'root_intent_ref', 'blockers'])
    expect(relations({ ...hint, source_refs: [objectRef(fact)] })[0]?.source).toBe(refKey(objectRef(fact)))
    const key = refKey({ object_id: 'id:"quoted",[]', revision: 2 })
    expect(JSON.parse(key)).toEqual(['id:"quoted",[]', 2])
    expect(projectKnowledgeGraph([fact], objectRef(intent)).edges).toHaveLength(0)
  })

  it('reads every page of a pinned unfiltered snapshot before publishing a graph', async () => {
    const items = Array.from({ length: 205 }, (_, index) => ({ ...hint, id: `hint-${index}` }))
    const snapshot = { ...summary, counts: { fact: 0, intent: 0, hint: 205 } }
    const mock = vi.spyOn(harnessApi, 'blackboardObjects')
      .mockResolvedValueOnce({ ...objectPage, total: 205, items: items.slice(0, 200), next_cursor: 'next-pinned' })
      .mockResolvedValueOnce({ ...objectPage, total: 205, items: items.slice(200), next_cursor: null })
    const signal = new AbortController().signal
    expect(await loadGraphObjects('board', snapshot, signal)).toHaveLength(205)
    expect(mock).toHaveBeenNthCalledWith(1, 'board', { cursor: summary.objects_cursor, limit: 200 }, signal)
    expect(mock).toHaveBeenNthCalledWith(2, 'board', { cursor: 'next-pinned', limit: 200 }, signal)
  })

  it('rejects partial or mixed snapshots and never presents them as a complete graph', async () => {
    const mock = vi.spyOn(harnessApi, 'blackboardObjects').mockResolvedValue({ ...objectPage, revision: 8 })
    await expect(loadGraphObjects('board', summary, new AbortController().signal)).rejects.toThrow('快照版本不一致')
    mock.mockResolvedValue({ ...objectPage, items: [fact] })
    await expect(loadGraphObjects('board', summary, new AbortController().signal)).rejects.toThrow('不完整')
    mock.mockResolvedValue({ ...objectPage, next_cursor: summary.objects_cursor })
    await expect(loadGraphObjects('board', summary, new AbortController().signal)).rejects.toThrow('游标重复')
  })
})
