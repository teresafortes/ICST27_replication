method ArrayToSeq(a: array<int>) returns (s: seq<int>)
    requires a != null
    ensures |s| == a.Length
    ensures forall i :: 0 <= i < a.Length ==> s[i] == a[i] && i != 0
{
    s := [];
    for i := 0 to a.Length
        invariant 0 <= i <= a.Length
        invariant |s| == i
        invariant forall j :: 0 <= j < i ==> s[j] == a[j]
    {
        s := s + [a[i]];
    }
}