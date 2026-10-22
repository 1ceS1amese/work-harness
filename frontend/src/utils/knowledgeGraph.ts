import type { BlackboardSummary, BoardObject, ObjectRef } from '../types'
import { harnessApi } from '../services/api'
import { objectText } from './blackboard'

export const RELATION_LABELS = {
  derived_from: '产生来源', contributes_to: '服务目标', refines: '细化', supports: '支持',
  contradicts: '反驳', supersedes: '替代', verification_premise: '核验前提',
  premise_refs: '声明前提', satisfied_by: '验收见证', blockers: '阻塞',
  parent_intent_ref: '父方向', root_intent_ref: '根目标', source_refs: '参考来源',
} as const
export type Relation = keyof typeof RELATION_LABELS
export interface KnowledgeEdge {
  key: string
  source: string
  target: string
  relation: Relation
  owner: ObjectRef
}
export interface KnowledgeNode {
  key: string
  ref: ObjectRef
  object: BoardObject | null
  current: boolean
  root: boolean
  x: number
  y: number
}
export const NODE_WIDTH = 210
export const NODE_HEIGHT = 118
export function refKey(ref: ObjectRef): string {
  return JSON.stringify([ref.object_id, ref.revision])
}
export function objectRef(object: BoardObject): ObjectRef {
  return { object_id: object.id, revision: object.revision }
}

export function relations(object: BoardObject): KnowledgeEdge[] {
  const owner = objectRef(object)
  const edges = new Map<string, KnowledgeEdge>()
  function add(relation: Relation, ref: ObjectRef, incoming = false): void {
    const source = refKey(incoming ? ref : owner)
    const target = refKey(incoming ? owner : ref)
    const key = JSON.stringify([relation, source, target])
    edges.set(key, { key, source, target, relation, owner })
  }
  object.derived_from.forEach(ref => add('derived_from', ref, true))
  object.links.forEach(link => add(link.relation, link.target))
  if (object.kind === 'fact') {
    object.verification.premise_refs.forEach(ref => add('verification_premise', ref, true))
    object.premise_refs.forEach(ref => add('premise_refs', ref, true))
  } else if (object.kind === 'intent') {
    if (object.parent_intent_ref) add('parent_intent_ref', object.parent_intent_ref)
    if (object.root_intent_ref) add('root_intent_ref', object.root_intent_ref)
    object.satisfied_by.forEach(ref => add('satisfied_by', ref, true))
    object.blockers.forEach(ref => add('blockers', ref, true))
  } else object.source_refs.forEach(ref => add('source_refs', ref, true))
  return [...edges.values()]
}

export function projectKnowledgeGraph(current: BoardObject[], root: ObjectRef, history: BoardObject[] = []) {
  const nodes = new Map<string, KnowledgeNode>()
  const edges = new Map<string, KnowledgeEdge>()
  const currentRefs = new Set(current.map(item => refKey(objectRef(item))))
  function ensure(ref: ObjectRef): KnowledgeNode {
    const key = refKey(ref)
    if (!nodes.has(key)) nodes.set(key, { key, ref, object: null, current: currentRefs.has(key), root: key === refKey(root), x: 0, y: 0 })
    return nodes.get(key)!
  }
  for (const object of [...history, ...current]) {
    ensure(objectRef(object)).object = object
    for (const edge of relations(object)) {
      // Keys encode refs losslessly, including IDs containing punctuation.
      const [sourceId, sourceRevision] = JSON.parse(edge.source) as [string, number]
      const [targetId, targetRevision] = JSON.parse(edge.target) as [string, number]
      ensure({ object_id: sourceId, revision: sourceRevision })
      ensure({ object_id: targetId, revision: targetRevision })
      edges.set(edge.key, edge)
    }
  }
  // Only production ancestry is a DAG. Support, conflict and goal links may
  // form cycles and must not be used as a topological execution plan.
  const children = new Map<string, string[]>()
  const degrees = new Map([...nodes.keys()].map(key => [key, 0]))
  const depths = new Map([...nodes.keys()].map(key => [key, 0]))
  for (const edge of edges.values()) {
    if (edge.relation !== 'derived_from') continue
    children.set(edge.source, [...(children.get(edge.source) ?? []), edge.target])
    degrees.set(edge.target, degrees.get(edge.target)! + 1)
  }
  const queue = [...nodes.keys()].filter(key => degrees.get(key) === 0).sort()
  for (let index = 0; index < queue.length; index += 1) {
    const key = queue[index]!
    for (const target of children.get(key) ?? []) {
      depths.set(target, Math.max(depths.get(target)!, depths.get(key)! + 1))
      degrees.set(target, degrees.get(target)! - 1)
      if (degrees.get(target) === 0) queue.push(target)
    }
  }
  const goalColumn = Math.max(0, ...depths.values()) + (nodes.size > 1 ? 1 : 0)
  const rows = new Map<number, number>()
  const sorted = [...nodes.values()].sort((a, b) => a.key.localeCompare(b.key))
  for (const node of sorted) {
    const column = node.root ? goalColumn : depths.get(node.key)!
    const row = rows.get(column) ?? 0
    node.x = 30 + column * 300
    node.y = 40 + row * 160
    rows.set(column, row + 1)
  }
  return { nodes: sorted, edges: [...edges.values()],
    width: (goalColumn + 1) * 300, height: Math.max(260, ...[...rows.values()].map(count => count * 160 + 40)),
    causalCycle: queue.length !== nodes.size }
}

export function nodeTitle(node: KnowledgeNode): string {
  return node.object ? objectText(node.object) : `未读取的历史引用 ${node.ref.object_id}`
}

export async function loadGraphObjects(runId: string, summary: BlackboardSummary, signal: AbortSignal): Promise<BoardObject[]> {
  let cursor: string | null = summary.objects_cursor
  const visited = new Set<string>()
  const objects = new Map<string, BoardObject>()
  const total = Object.values(summary.counts).reduce((sum, count) => sum + count, 0)
  while (cursor) {
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    if (visited.has(cursor)) throw new Error('图数据分页游标重复，请刷新。')
    visited.add(cursor)
    const page = await harnessApi.blackboardObjects(runId, { cursor, limit: 200 }, signal)
    if (page.board_id !== summary.board_id || page.revision !== summary.revision || page.total !== total) {
      throw new Error('图数据与目标的快照版本不一致，请刷新。')
    }
    for (const object of page.items) {
      if (objects.has(object.id)) throw new Error('图数据包含重复对象，请刷新。')
      objects.set(object.id, object)
    }
    cursor = page.next_cursor
  }
  if (objects.size !== total) throw new Error('图数据不完整，请刷新。')
  return [...objects.values()]
}
